from __future__ import annotations

import json
from datetime import datetime, timezone
from io import BytesIO, StringIO
from pathlib import Path

import pytest

from backend.services.reference_clip_library import ReferenceClipLibrary
from backend.services.reference_discovery import (
    MediaEvidence, ReferenceCandidateQueue, ReferenceDiscoveryError,
    ReferenceDiscoveryService, YouTubeDataAPI, infer_topic, main,
    parse_iso_duration, score_candidate, select_diverse, views_per_day,
)


NOW = datetime(2026, 7, 25, tzinfo=timezone.utc)


class Response(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def api_opener(documents):
    pending = iter(documents)

    def open_request(request, timeout):
        assert "key=private-key" in request.full_url
        assert timeout == 30
        return Response(json.dumps(next(pending)).encode())

    return open_request


def candidate(
    video_id="one", *, creator="Creator", title="Minecraft clutch",
    topic=None, views=1_000_000, likes=50_000, comments=5_000,
    published="2026-07-20T00:00:00Z", duration=45,
):
    return {
        "video_id": video_id,
        "source_url": f"https://www.youtube.com/watch?v={video_id}",
        "title": title, "creator": creator, "channel_id": f"channel-{creator}",
        "published_at": published, "view_count": views, "like_count": likes,
        "comment_count": comments, "duration": duration,
        "verified_duration": duration, "verified_vertical": True,
        "width": 1080, "height": 1920, "frame_rate": 60.0,
        "has_video": True, "has_audio": True, "downloadable": True,
        "media_path": None, "validation_error": None,
        "discovery_query": "gaming funny moments shorts",
        "captured_at": "2026-07-25T00:00:00Z", "topic": topic,
    }


def test_api_response_parsing_and_metadata_hydration():
    api = YouTubeDataAPI(
        "private-key",
        opener=api_opener(
            [
                {
                    "items": [
                        {
                            "id": {"videoId": "one"},
                            "snippet": {"title": "Result"},
                        }
                    ]
                },
                {
                    "items": [
                        {
                            "id": "one",
                            "snippet": {
                                "title": "A short", "channelTitle": "Creator",
                                "channelId": "channel", "publishedAt": "2026-07-20T00:00:00Z",
                            },
                            "contentDetails": {"duration": "PT45S"},
                            "statistics": {
                                "viewCount": "1000", "likeCount": "50",
                                "commentCount": "4",
                            },
                        }
                    ]
                },
            ]
        ),
    )
    search = api.search(
        ["gaming"], pool_size=20,
        published_after="2026-01-01T00:00:00Z", region="US",
    )
    hydrated = api.hydrate(search)
    assert search == [
        {"video_id": "one", "discovery_query": "gaming", "search_title": "Result"}
    ]
    assert hydrated[0]["duration"] == 45
    assert hydrated[0]["view_count"] == 1000
    assert hydrated[0]["like_count"] == 50
    assert hydrated[0]["comment_count"] == 4


def test_missing_api_key_is_actionable(monkeypatch):
    monkeypatch.delenv("YOUTUBE_DATA_API_KEY", raising=False)
    api = YouTubeDataAPI(api_key=None, environment_file=Path("/missing"))
    with pytest.raises(ReferenceDiscoveryError, match="YOUTUBE_DATA_API_KEY"):
        api.search(
            ["gaming"], pool_size=10,
            published_after="2026-01-01T00:00:00Z", region="US",
        )


def test_api_key_loads_from_private_environment_file(tmp_path, monkeypatch):
    monkeypatch.delenv("YOUTUBE_DATA_API_KEY", raising=False)
    environment = tmp_path / "creatorflow.env"
    environment.write_text(
        "AUTOCLIP_PRODUCTION_ARGS=\nYOUTUBE_DATA_API_KEY=private-key\n",
        encoding="utf-8",
    )
    environment.chmod(0o600)
    api = YouTubeDataAPI(environment_file=environment)
    assert api.api_key == "private-key"


def test_api_key_file_rejects_broad_permissions(tmp_path, monkeypatch):
    monkeypatch.delenv("YOUTUBE_DATA_API_KEY", raising=False)
    environment = tmp_path / "creatorflow.env"
    environment.write_text("YOUTUBE_DATA_API_KEY=private-key\n", encoding="utf-8")
    environment.chmod(0o644)
    with pytest.raises(ReferenceDiscoveryError, match="mode 0600"):
        YouTubeDataAPI(environment_file=environment)


@pytest.mark.parametrize(
    ("value", "seconds"),
    [("PT45S", 45), ("PT1M30S", 90), ("PT2H3M4S", 7384)],
)
def test_duration_parsing(value, seconds):
    assert parse_iso_duration(value) == seconds


def test_views_per_day_and_ranking_components():
    item = candidate()
    assert views_per_day(1_000_000, item["published_at"], now=NOW) == 200_000
    score, explanation = score_candidate(item, now=NOW)
    assert score > 0
    assert explanation["version"] == 1
    assert set(explanation["components"]) == {
        "raw_views", "views_per_day", "like_ratio", "comment_ratio",
        "recency", "duration", "vertical", "creator_diversity",
        "topic_diversity", "evidence_penalty",
    }
    assert "not a prediction" in explanation["evidence"]


def test_unavailable_engagement_and_evidence_are_penalized():
    complete, _ = score_candidate(candidate(), now=NOW)
    missing, explanation = score_candidate(
        candidate(likes=None, comments=None) | {"verified_vertical": None},
        now=NOW,
    )
    assert missing < complete
    assert explanation["components"]["evidence_penalty"] == -12


def test_media_evidence_requires_vertical_short_with_video_and_audio():
    valid = MediaEvidence(True, 45, 1080, 1920, 60, True, True)
    landscape = MediaEvidence(True, 45, 1920, 1080, 60, True, True)
    long = MediaEvidence(True, 240, 1080, 1920, 60, True, True)
    assert valid.vertical is True and valid.valid_short
    assert landscape.vertical is False and not landscape.valid_short
    assert not long.valid_short


def test_diversity_caps_and_title_deduplication():
    items = [
        candidate("a", creator="Same", title="Fortnite clutch"),
        candidate("b", creator="Same", title="Valorant clutch", views=900_000),
        candidate("c", creator="Same", title="Roblox clutch", views=800_000),
        candidate("d", creator="Other", title="Minecraft clutch four", views=700_000),
        candidate("e", creator="Third", title="Minecraft clutch five", views=600_000),
        candidate("g", creator="Sixth", title="Minecraft clutch six", views=550_000),
        candidate("h", creator="Seventh", title="Minecraft clutch seven", views=525_000),
        candidate("f", creator="Fourth", title="Apex ace", views=500_000),
        candidate("duplicate", creator="Fifth", title="Apex ace", views=400_000),
    ]
    selected = select_diverse(
        items, count=20, max_per_creator=2, max_per_topic=3, now=NOW
    )
    assert sum(item["creator"] == "Same" for item in selected) == 2
    assert sum(item["topic"] == "minecraft" for item in selected) == 3
    assert len([item for item in selected if item["title"] == "Apex ace"]) == 1


def test_topic_inference_is_transparent():
    assert infer_topic("Wild Fortnite win", "gaming shorts") == "fortnite"


class FakeAPI:
    def __init__(self, items):
        self.items = items

    def search(self, queries, **_kwargs):
        return [
            {"video_id": item["video_id"], "discovery_query": queries[0]}
            for item in self.items
        ]

    def hydrate(self, _search):
        return [dict(item) for item in self.items]


class FakeValidator:
    def __init__(self, media_path=None):
        self.calls = []
        self.media_path = media_path

    def validate(self, item, *, retain=True):
        self.calls.append((item["video_id"], retain))
        return MediaEvidence(
            True, 45, 1080, 1920, 60, True, True,
            str(self.media_path) if self.media_path else None,
        )


def test_fixture_backed_discovery_creates_separate_review_queue(tmp_path):
    queue = ReferenceCandidateQueue(tmp_path / "candidates.json")
    validator = FakeValidator()
    items = [candidate("one"), candidate("two", creator="Other", title="Valorant ace")]
    service = ReferenceDiscoveryService(
        FakeAPI(items), queue, media_validator=validator,
    )
    result = service.discover(target_count=2, pool_size=10)
    assert result["selected_count"] == 2
    assert len(queue.list(status="discovered")) == 2
    assert all(item["origin"] == "automatic_youtube_discovery" for item in queue.list())


def test_dry_run_does_not_validate_or_mutate_queue(tmp_path):
    queue = ReferenceCandidateQueue(tmp_path / "candidates.json")
    validator = FakeValidator()
    service = ReferenceDiscoveryService(
        FakeAPI([candidate()]), queue, media_validator=validator,
    )
    result = service.discover(dry_run=True, target_count=1)
    assert result["dry_run"] is True
    assert validator.calls == []
    assert queue.list() == []
    assert not queue.path.exists()


def test_repeat_discovery_deduplicates_and_preserves_rejection(tmp_path):
    queue = ReferenceCandidateQueue(tmp_path / "candidates.json")
    service = ReferenceDiscoveryService(
        FakeAPI([candidate()]), queue, media_validator=FakeValidator(),
    )
    service.discover(target_count=1)
    queue.decide("one", "rejected", notes="not useful")
    service.discover(target_count=1)
    assert len(queue.list()) == 1
    assert queue.get("one")["status"] == "rejected"
    assert queue.get("one")["notes"] == "not useful"


def test_refresh_stats_preserves_review_decision_and_annotations(tmp_path):
    queue = ReferenceCandidateQueue(tmp_path / "candidates.json")
    queue.upsert_discovered([candidate()])
    queue.decide(
        "one", "rejected", notes="weak setup", category="personality_reaction"
    )
    refreshed = candidate(views=2_000_000, likes=None, comments=None)
    service = ReferenceDiscoveryService(FakeAPI([refreshed]), queue)
    assert service.refresh_stats() == 1
    item = queue.get("one")
    assert item["view_count"] == 2_000_000
    assert item["status"] == "rejected"
    assert item["notes"] == "weak setup"
    assert item["category"] == "personality_reaction"


def test_atomic_queue_write_leaves_no_temporary_file(tmp_path):
    queue = ReferenceCandidateQueue(tmp_path / "candidates.json")
    queue.upsert_discovered([candidate()])
    assert queue.get("one")["video_id"] == "one"
    assert not list(tmp_path.glob(".*.tmp"))


class FakeAnalyzer:
    calls = []

    def __init__(self, _library):
        pass

    def analyze(self, reference_id, *, transcription):
        self.calls.append((reference_id, transcription))
        return {"reference_id": reference_id}


class FailingAnalyzer:
    def __init__(self, _library):
        pass

    def analyze(self, _reference_id, *, transcription):
        raise ReferenceDiscoveryError("analysis failed")


def test_acceptance_uses_strict_reference_registration_and_analysis(tmp_path):
    media = tmp_path / "candidate.mp4"
    media.write_bytes(b"local-media")
    queue = ReferenceCandidateQueue(tmp_path / "candidates.json")
    item = candidate() | {"media_path": str(media), "rank": 1}
    queue.upsert_discovered([item])
    library = ReferenceClipLibrary(tmp_path / "references")
    FakeAnalyzer.calls = []
    service = ReferenceDiscoveryService(
        FakeAPI([]), queue, media_validator=FakeValidator(media),
        reference_library=library, analyzer_factory=FakeAnalyzer,
        reference_root=tmp_path / "references",
    )
    entry = service.accept(
        "one", category="gaming_highlight", notes="Strong human-reviewed beat"
    )
    assert entry["reference_id"] == "youtube-one"
    assert entry["status"] == "accepted"
    assert library.list_references(status="accepted") == [entry]
    assert FakeAnalyzer.calls == [("youtube-one", False)]
    assert queue.get("one")["status"] == "accepted"
    source = json.loads(Path(entry["source_info_path"]).read_text())
    assert source["origin"] == "automatic_youtube_discovery"


def test_failed_analysis_rolls_back_reference_and_preserves_candidate(tmp_path):
    media = tmp_path / "candidate.mp4"
    media.write_bytes(b"local-media")
    queue = ReferenceCandidateQueue(tmp_path / "candidates.json")
    queue.upsert_discovered([candidate() | {"media_path": str(media), "rank": 1}])
    reference_root = tmp_path / "references"
    library = ReferenceClipLibrary(reference_root)
    service = ReferenceDiscoveryService(
        FakeAPI([]), queue, media_validator=FakeValidator(media),
        reference_library=library, analyzer_factory=FailingAnalyzer,
        reference_root=reference_root,
    )
    with pytest.raises(ReferenceDiscoveryError, match="analysis failed"):
        service.accept("one", category="gaming_highlight", notes="candidate")
    assert queue.get("one")["status"] == "discovered"
    assert library.list_references() == []
    assert not (reference_root / "discovered-one").exists()


def test_rejection_never_registers_or_influences_references(tmp_path):
    queue = ReferenceCandidateQueue(tmp_path / "candidates.json")
    queue.upsert_discovered([candidate()])
    queue.decide("one", "rejected", notes="repost")
    library = ReferenceClipLibrary(tmp_path / "references")
    assert library.list_references(status="accepted") == []
    assert queue.get("one")["status"] == "rejected"


def test_cli_missing_key_returns_nonzero_without_exposing_value(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("YOUTUBE_DATA_API_KEY", raising=False)
    result = main([
        "--queue-path", str(tmp_path / "queue.json"),
        "discover", "--dry-run", "--target-count", "1",
    ])
    output = capsys.readouterr()
    assert result == 1
    assert "YOUTUBE_DATA_API_KEY" in output.err
    assert "private-key" not in output.err
