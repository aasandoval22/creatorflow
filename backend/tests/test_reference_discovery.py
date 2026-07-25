from __future__ import annotations

import json
from datetime import datetime, timezone
from io import BytesIO, StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.services.reference_clip_library import ReferenceClipLibrary
from backend.services.reference_discovery import (
    LocalMediaValidator, MediaEvidence, ReferenceCandidateQueue,
    ReferenceDiscoveryError, ReferenceDiscoveryService, YouTubeDataAPI,
    evaluate_gaming_relevance, evaluate_source_quality, infer_topic, main,
    near_duplicate_title, parse_iso_duration, qualify_metadata_candidates,
    resolve_cohort_counts, score_candidate, select_benchmark_cohorts,
    select_diverse, views_per_day,
)
from backend.services.youtube_downloader import YouTubeDownloader


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
    description="", tags=None, category_id="20",
    query="gaming funny moments shorts",
):
    return {
        "video_id": video_id,
        "source_url": f"https://www.youtube.com/watch?v={video_id}",
        "title": title, "creator": creator, "channel_id": f"channel-{creator}",
        "channel_title": creator, "description": description,
        "tags": list(tags or []), "category_id": category_id,
        "published_at": published, "view_count": views, "like_count": likes,
        "comment_count": comments, "duration": duration,
        "verified_duration": duration, "verified_vertical": True,
        "width": 1080, "height": 1920, "frame_rate": 60.0,
        "has_video": True, "has_audio": True, "downloadable": True,
        "media_path": None, "validation_error": None,
        "discovery_query": query,
        "captured_at": "2026-07-25T00:00:00Z", "topic": topic,
    }


def executable_deno(tmp_path, name="deno"):
    path = tmp_path / ".deno" / "bin" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def test_reference_validation_and_downloader_share_explicit_deno_options(
    tmp_path,
):
    deno = executable_deno(tmp_path)
    validator = LocalMediaValidator(tmp_path / "media", deno_path=deno)
    downloader = YouTubeDownloader(discovery_only=True, deno_path=deno)
    expected = {"deno": {"path": str(deno)}}

    assert validator._download_options(tmp_path / "candidate.mp4")[
        "js_runtimes"
    ] == expected
    assert downloader._metadata_options()["js_runtimes"] == expected
    assert downloader._build_options()["js_runtimes"] == expected


def test_reference_validation_detects_standard_deno_without_shell_path(
    tmp_path, monkeypatch,
):
    deno = executable_deno(tmp_path)
    monkeypatch.setattr(
        "backend.services.youtube_downloader.DEFAULT_DENO_PATH", deno
    )
    monkeypatch.delenv("AUTOCLIP_DENO_PATH", raising=False)
    monkeypatch.setattr(
        "backend.services.youtube_downloader.shutil.which", lambda _name: None
    )

    validator = LocalMediaValidator(tmp_path / "media")

    assert validator.deno_path == deno
    assert validator._download_options(tmp_path / "candidate.mp4")[
        "js_runtimes"
    ] == {"deno": {"path": str(deno)}}


def test_reference_validation_detects_environment_deno(tmp_path, monkeypatch):
    deno = executable_deno(tmp_path)
    monkeypatch.setenv("AUTOCLIP_DENO_PATH", str(deno))
    monkeypatch.setattr(
        "backend.services.youtube_downloader.DEFAULT_DENO_PATH",
        tmp_path / "missing-standard-deno",
    )
    monkeypatch.setattr(
        "backend.services.youtube_downloader.shutil.which", lambda _name: None
    )

    validator = LocalMediaValidator(tmp_path / "media")

    assert validator.deno_path == deno
    assert validator._download_options(tmp_path / "candidate.mp4")[
        "js_runtimes"
    ] == {"deno": {"path": str(deno)}}


def test_reference_validation_missing_deno_warns_and_continues(
    tmp_path, monkeypatch,
):
    monkeypatch.delenv("AUTOCLIP_DENO_PATH", raising=False)
    monkeypatch.setattr(
        "backend.services.youtube_downloader.DEFAULT_DENO_PATH",
        tmp_path / "missing-standard-deno",
    )
    monkeypatch.setattr(
        "backend.services.youtube_downloader.shutil.which", lambda _name: None
    )

    with pytest.warns(RuntimeWarning, match="Deno JavaScript runtime was not found"):
        validator = LocalMediaValidator(tmp_path / "media")

    assert validator.deno_path is None
    assert "js_runtimes" not in validator._download_options(
        tmp_path / "candidate.mp4"
    )


def test_reference_validation_preserves_symlinked_deno_path(
    tmp_path, monkeypatch,
):
    target = executable_deno(tmp_path, "deno-real")
    configured = target.with_name("deno-link")
    configured.symlink_to(target)
    monkeypatch.setenv("AUTOCLIP_DENO_PATH", str(configured))

    validator = LocalMediaValidator(tmp_path / "media")

    assert validator.deno_path == configured
    assert validator.deno_path != configured.resolve()
    assert validator._download_options(tmp_path / "candidate.mp4")[
        "js_runtimes"
    ] == {"deno": {"path": str(configured)}}


def test_reference_youtube_dl_call_includes_shared_runtime_options(tmp_path):
    deno = executable_deno(tmp_path)
    captured = []

    class FakeYoutubeDL:
        def __init__(self, options):
            captured.append(options)
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def download(self, urls):
            assert urls == ["https://www.youtube.com/watch?v=one"]
            Path(self.options["outtmpl"]).write_bytes(b"fixture-media")
            return 0

    probe = SimpleNamespace(
        returncode=0,
        stdout=json.dumps(
            {
                "streams": [
                    {
                        "codec_type": "video",
                        "width": 1080,
                        "height": 1920,
                        "avg_frame_rate": "60/1",
                    },
                    {"codec_type": "audio"},
                ],
                "format": {"duration": "45"},
            }
        ),
        stderr="",
    )
    validator = LocalMediaValidator(tmp_path / "media", deno_path=deno)

    with (
        patch(
            "backend.services.reference_discovery.yt_dlp.YoutubeDL",
            FakeYoutubeDL,
        ),
        patch(
            "backend.services.reference_discovery.subprocess.run",
            return_value=probe,
        ),
    ):
        evidence = validator.validate(candidate(), retain=False)

    assert evidence.valid_short
    assert len(captured) == 1
    assert captured[0]["js_runtimes"] == {
        "deno": {"path": str(deno)}
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
                                "description": "Minecraft gameplay",
                                "tags": ["minecraft", "gaming"],
                                "categoryId": "20",
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
    assert hydrated[0]["description"] == "Minecraft gameplay"
    assert hydrated[0]["tags"] == ["minecraft", "gaming"]
    assert hydrated[0]["category_id"] == "20"
    assert hydrated[0]["channel_title"] == "Creator"


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
    assert explanation["version"] == 2
    assert explanation["cohort"] == "established"
    assert set(explanation["components"]) == {
        "raw_views", "views_per_day", "like_ratio", "comment_ratio",
        "recency", "duration", "vertical", "creator_diversity",
        "topic_diversity", "gaming_relevance", "source_quality_penalty",
        "evidence_penalty",
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


@pytest.mark.parametrize(
    ("title", "description", "tags", "expected"),
    [
        ("Top 3 Funniest FNAF Moments", "", [], "fnaf"),
        ("Scariest clip", "Five Nights at Freddy's gameplay", [], "fnaf"),
        ("Roblox obby disaster", "", [], "roblox"),
        ("COD clutch", "", [], "call-of-duty"),
        ("Raid reaction", "Destiny 2 boss fight", [], "destiny-2"),
    ],
)
def test_game_aliases_produce_useful_topics(
    title, description, tags, expected,
):
    assert infer_topic(
        candidate(
            title=title,
            description=description,
            tags=tags,
        )
    ) == expected


def test_verified_gaming_without_known_game_uses_unknown_topic():
    item = candidate(
        title="Unbelievable final boss",
        description="Original gameplay highlight",
        tags=["gaming"],
    )
    relevance = evaluate_gaming_relevance(item)
    assert relevance["eligible"] is True
    assert relevance["topic"] == "unknown-gaming"
    assert infer_topic(item) == "unknown-gaming"


def test_topic_inference_prefers_title_over_conflicting_lower_priority_metadata():
    item = candidate(
        title="Trick shots in Roblox Rivals",
        description="Creator also posts Call of Duty",
        tags=["call of duty", "roblox"],
    )
    assert infer_topic(item) == "roblox"


def test_query_only_mrbeast_animal_challenge_is_not_gaming():
    item = candidate(
        title="MrBeast — Guess the Animal",
        creator="MrBeast",
        description="Can these celebrities identify every animal?",
        tags=["animals", "challenge"],
        category_id="24",
        query="gaming challenge shorts",
    )
    relevance = evaluate_gaming_relevance(item)
    assert relevance["eligible"] is False
    assert "no corroborating gaming metadata" in relevance["evidence"]
    assert relevance["topic"] is None


def test_generic_streamer_reaction_without_game_evidence_is_rejected():
    item = candidate(
        title="Streamer reacts to a wild surprise",
        description="You will not believe this reaction",
        tags=["reaction", "streamer"],
        category_id="24",
        query="streamer reaction gaming shorts",
    )
    relevance = evaluate_gaming_relevance(item)
    assert relevance["eligible"] is False
    assert "Entertainment-category" in relevance["exclusion_reasons"][0]


def test_non_gaming_title_cannot_be_overridden_by_spam_metadata():
    item = candidate(
        title="Jumbo Crazy Ball cricket unboxing",
        description="Minecraft gamer gaming",
        tags=["minecraft", "gaming"],
        category_id="22",
        query="gaming funny moments shorts",
    )
    relevance = evaluate_gaming_relevance(item)
    assert relevance["eligible"] is False
    assert "explicit unrelated subject" in relevance["exclusion_reasons"][-1]
    assert "cricket" in relevance["evidence"]


def test_explicit_game_reaction_in_entertainment_remains_eligible():
    item = candidate(
        title="Streamer loses it during a FNAF jumpscare",
        description="Five Nights at Freddy's gameplay reaction",
        tags=["fnaf", "gameplay"],
        category_id="24",
        query="horror game reaction shorts",
    )
    relevance = evaluate_gaming_relevance(item)
    assert relevance["eligible"] is True
    assert relevance["topic"] == "fnaf"
    assert any("Recognized game metadata" in value for value in relevance["positive_evidence"])


def test_category_twenty_is_strong_gaming_evidence_not_a_topic_guess():
    item = candidate(
        title="Top funniest reaction ever",
        description="",
        tags=[],
        category_id="20",
    )
    relevance = evaluate_gaming_relevance(item)
    assert relevance["eligible"] is True
    assert relevance["topic"] == "unknown-gaming"
    assert infer_topic(item) not in {
        "top", "funniest", "reaction", "guess", "ranking", "even",
    }


def test_ranking_is_penalized_but_not_automatically_rejected():
    item = candidate(
        title="Top 3 Funniest FNAF Moments",
        description="My own FNAF gameplay ranked",
        tags=["fnaf", "gaming"],
    )
    quality = evaluate_source_quality(item)
    assert quality["eligible"] is True
    assert quality["status"] == "derivative-risk"
    assert quality["penalty"] < 0
    assert "Ranking-format" in quality["evidence"]


def test_explicit_repost_and_multi_creator_ranking_are_excluded():
    repost = evaluate_source_quality(
        candidate(
            title="Fortnite clutch repost",
            description="Credit to original creator",
        )
    )
    ranking = evaluate_source_quality(
        candidate(
            title="Top 5 Minecraft clips",
            description="Clips from different creators",
        )
    )
    assert repost["eligible"] is False
    assert ranking["eligible"] is False
    assert ranking["exclusion_reasons"] == [
        "Ranking appears assembled from multiple creators."
    ]


def test_near_duplicate_titles_keep_preferred_original_source():
    original = candidate(
        "original",
        title="Fortnite impossible ranked clutch",
        views=500_000,
    )
    derivative = candidate(
        "derivative",
        title="Fortnite impossible ranked clutch!",
        views=2_000_000,
        description="Compilation",
    )
    qualified, excluded = qualify_metadata_candidates(
        [derivative, original]
    )
    assert [item["video_id"] for item in qualified] == ["original"]
    assert excluded[0]["video_id"] == "derivative"
    assert excluded[0]["stage"] == "deduplication"
    assert near_duplicate_title(original["title"], derivative["title"])


def test_established_and_breakout_cohorts_use_distinct_emphasis():
    items = [
        candidate(
            "established-a", creator="Established A", topic="fortnite",
            title="Fortnite tournament clutch",
            views=80_000_000, published="2026-01-01T00:00:00Z",
        ),
        candidate(
            "established-b", creator="Established B", topic="minecraft",
            title="Minecraft hardcore escape",
            views=60_000_000, published="2026-02-01T00:00:00Z",
        ),
        candidate(
            "breakout-a", creator="Breakout A", topic="roblox",
            title="Roblox obby speedrun",
            views=1_000_000, published="2026-07-24T12:00:00Z",
        ),
        candidate(
            "breakout-b", creator="Breakout B", topic="fnaf",
            title="FNAF jumpscare reaction",
            views=800_000, published="2026-07-24T18:00:00Z",
        ),
    ]
    qualified, excluded = qualify_metadata_candidates(items)
    selection = select_benchmark_cohorts(
        qualified,
        established_count=2,
        breakout_count=2,
        now=NOW,
    )
    assert excluded == []
    assert {item["cohort"] for item in selection.selected} == {
        "established", "breakout",
    }
    assert sum(item["cohort"] == "established" for item in selection.selected) == 2
    assert sum(item["cohort"] == "breakout" for item in selection.selected) == 2
    established_score = score_candidate(
        qualified[0], cohort="established", now=NOW
    )[1]["components"]
    breakout_score = score_candidate(
        qualified[0], cohort="breakout", now=NOW
    )[1]["components"]
    assert established_score["raw_views"] > breakout_score["raw_views"]
    assert breakout_score["views_per_day"] > established_score["views_per_day"]


def test_default_and_balanced_target_cohort_sizes():
    assert resolve_cohort_counts(
        target_count=None,
        established_count=None,
        breakout_count=None,
    ) == (10, 10)
    assert resolve_cohort_counts(
        target_count=10,
        established_count=None,
        breakout_count=None,
    ) == (5, 5)


def test_creator_and_topic_caps_apply_across_both_cohorts():
    moments = ("castle", "cave", "dragon", "village", "nether", "end")
    items = [
        candidate(
            f"same-{index}",
            creator="Same Creator",
            title=f"Minecraft {moments[index]} clutch",
            topic="minecraft",
            views=10_000_000 - index,
        )
        for index in range(6)
    ] + [
        candidate(
            "other-a", creator="Other A", title="Fortnite clutch",
            topic="fortnite",
        ),
        candidate(
            "other-b", creator="Other B", title="Roblox clutch",
            topic="roblox",
        ),
    ]
    qualified, _excluded = qualify_metadata_candidates(items)
    selection = select_benchmark_cohorts(
        qualified,
        established_count=2,
        breakout_count=2,
        max_per_creator=2,
        max_per_topic=3,
        now=NOW,
    )
    assert sum(
        item["creator"] == "Same Creator" for item in selection.selected
    ) <= 2
    assert sum(
        item["topic"] == "minecraft" for item in selection.selected
    ) <= 3
    assert any(
        "Creator diversity cap" in item["reason"]
        for item in selection.excluded
    )


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
    assert result["cohorts"] == {"established": 1, "breakout": 1}
    assert result["validation_summary"]["media_verified"] == 2
    assert len(queue.list(status="discovered")) == 2
    assert all(
        item["origin"] == "automatic_youtube_discovery"
        and item["validation_status"] == "media-verified"
        for item in queue.list()
    )


def test_dry_run_does_not_validate_or_mutate_queue(tmp_path):
    queue = ReferenceCandidateQueue(tmp_path / "candidates.json")
    validator = FakeValidator()
    service = ReferenceDiscoveryService(
        FakeAPI([candidate()]), queue, media_validator=validator,
    )
    result = service.discover(dry_run=True, target_count=1)
    assert result["dry_run"] is True
    assert result["validation_summary"] == {
        "metadata_qualified": 1,
        "media_verified": 0,
        "rejected_during_media_validation": 0,
        "media_verification": "provisional",
    }
    assert result["selected"][0]["validation_status"] == "metadata-qualified"
    assert result["selected"][0]["media_verification"] == "provisional"
    assert validator.calls == []
    assert queue.list() == []
    assert not queue.path.exists()


def test_dry_run_excludes_query_only_false_positive_before_ranking(tmp_path):
    queue = ReferenceCandidateQueue(tmp_path / "candidates.json")
    false_positive = candidate(
        "animal",
        creator="MrBeast",
        title="Guess the Animal",
        description="Celebrity animal challenge",
        tags=["animals", "challenge"],
        category_id="24",
        query="gaming challenge shorts",
    )
    game = candidate(
        "fnaf",
        creator="Game Creator",
        title="Top 3 Funniest FNAF Moments",
        description="Original FNAF gameplay",
        tags=["fnaf", "gaming"],
    )
    service = ReferenceDiscoveryService(
        FakeAPI([false_positive, game]),
        queue,
        media_validator=FakeValidator(),
    )
    result = service.discover(dry_run=True, target_count=1)
    assert [item["video_id"] for item in result["selected"]] == ["fnaf"]
    assert result["selected"][0]["topic"] == "fnaf"
    exclusion = next(
        item for item in result["exclusions"]
        if item["video_id"] == "animal"
    )
    assert exclusion["stage"] == "gaming-relevance"
    assert "lacks explicit gaming metadata" in exclusion["reason"]


def test_explicit_cohort_sizes_are_configurable_in_dry_run(tmp_path):
    queue = ReferenceCandidateQueue(tmp_path / "candidates.json")
    items = [
        candidate("one", title="Minecraft clutch"),
        candidate("two", title="Roblox speedrun", creator="Other"),
    ]
    service = ReferenceDiscoveryService(
        FakeAPI(items),
        queue,
        media_validator=FakeValidator(),
    )
    result = service.discover(
        dry_run=True,
        established_count=0,
        breakout_count=2,
    )
    assert result["cohorts"] == {"established": 0, "breakout": 2}
    assert all(item["cohort"] == "breakout" for item in result["selected"])


class SelectiveValidator:
    def __init__(self, rejected_id):
        self.rejected_id = rejected_id

    def validate(self, item, *, retain=True):
        if item["video_id"] == self.rejected_id:
            return MediaEvidence(
                False,
                error="fixture has no audio stream",
            )
        return MediaEvidence(
            True, 45, 1080, 1920, 60, True, True,
        )


def test_real_stage_reranks_only_media_verified_candidates(tmp_path):
    queue = ReferenceCandidateQueue(tmp_path / "candidates.json")
    items = [
        candidate("invalid", title="Fortnite record clutch", views=9_000_000),
        candidate("valid-a", title="Minecraft clutch", creator="A"),
        candidate("valid-b", title="Roblox clutch", creator="B"),
    ]
    service = ReferenceDiscoveryService(
        FakeAPI(items),
        queue,
        media_validator=SelectiveValidator("invalid"),
    )
    result = service.discover(target_count=2, pool_size=10)
    queued_ids = {item["video_id"] for item in queue.list()}
    assert result["validation_summary"]["rejected_during_media_validation"] == 1
    assert queued_ids == {"valid-a", "valid-b"}
    assert all(
        item["validation_status"] == "media-verified"
        for item in result["selected"]
    )
    rejection = next(
        item for item in result["exclusions"]
        if item["video_id"] == "invalid"
    )
    assert rejection["validation_status"] == (
        "rejected during media validation"
    )


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


def test_manual_topic_correction_survives_rediscovery_and_refresh(tmp_path):
    queue = ReferenceCandidateQueue(tmp_path / "candidates.json")
    queue.upsert_discovered([candidate()])
    queue.decide("one", "discovered", topic="manual-game")
    service = ReferenceDiscoveryService(
        FakeAPI([candidate(title="Fortnite clutch", views=2_000_000)]),
        queue,
        media_validator=FakeValidator(),
    )
    service.discover(target_count=1)
    service.refresh_stats()
    item = queue.get("one")
    assert item["topic"] == "manual-game"
    assert item["topic_manually_corrected"] is True


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
        "one", category="gaming_highlight",
        notes="Strong human-reviewed beat", topic="manual-game",
    )
    assert entry["reference_id"] == "youtube-one"
    assert entry["status"] == "accepted"
    assert library.list_references(status="accepted") == [entry]
    assert FakeAnalyzer.calls == [("youtube-one", False)]
    assert queue.get("one")["status"] == "accepted"
    assert queue.get("one")["topic"] == "manual-game"
    source = json.loads(Path(entry["source_info_path"]).read_text())
    assert source["origin"] == "automatic_youtube_discovery"
    assert source["metadata_snapshot"]["topic"] == "manual-game"


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
    with patch(
        "backend.services.reference_discovery._private_environment_value",
        return_value=None,
    ):
        result = main([
            "--queue-path", str(tmp_path / "queue.json"),
            "discover", "--dry-run", "--target-count", "1",
        ])
    output = capsys.readouterr()
    assert result == 1
    assert "YOUTUBE_DATA_API_KEY" in output.err
    assert "private-key" not in output.err
