import json
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.services.clip_candidate_generator import (
    AnalysisResultStatus,
    CandidateConfiguration,
    ClipCandidateGenerator,
    TranscriptError,
)
from backend.services.video_manifest import (
    ClipAnalysisStatus,
    TranscriptionStatus,
    VideoManifest,
    default_clip_analysis,
    default_transcription,
)


def manifest_record(video_id: str, transcript_path: Path) -> dict:
    transcription = default_transcription()
    transcription.update(
        status=TranscriptionStatus.COMPLETED.value,
        transcript_json_path=str(transcript_path),
    )
    return {
        "video_id": video_id,
        "source_platform": "youtube",
        "channel_name": "Creator",
        "channel_url": "https://example.test/creator",
        "video_url": f"https://example.test/{video_id}",
        "title": "Test",
        "uploader": "Creator",
        "upload_date": "20260724",
        "duration_seconds": 90,
        "discovered_at": "2026-07-24T12:00:00+00:00",
        "downloaded_at": "2026-07-24T12:01:00+00:00",
        "local_file_path": f"/tmp/{video_id}.mp4",
        "status": "downloaded",
        "error_message": None,
        "transcription": transcription,
        "clip_analysis": default_clip_analysis(),
    }


def segments(count=8, seconds=6):
    return [
        {
            "id": index,
            "start": index * seconds,
            "end": (index + 1) * seconds,
            "text": (
                "Here is the reason you should choose the best option. "
                "It has three concrete benefits."
            ),
        }
        for index in range(count)
    ]


def write_transcript(path: Path, video_id="abc", content=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "version": 1,
        "video_id": video_id,
        "source_media_path": "/tmp/source.mp4",
        "segments": segments() if content is None else content,
    }
    path.write_text(json.dumps(document), encoding="utf-8")


def test_validates_missing_invalid_and_empty_transcripts(tmp_path):
    with pytest.raises(TranscriptError, match="does not exist"):
        ClipCandidateGenerator.load_transcript(tmp_path / "missing.json")
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    with pytest.raises(TranscriptError, match="Invalid transcript JSON"):
        ClipCandidateGenerator.load_transcript(broken)
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"version": 2, "segments": []}), encoding="utf-8")
    with pytest.raises(TranscriptError, match="version 1"):
        ClipCandidateGenerator.load_transcript(wrong)
    empty = tmp_path / "empty.json"
    write_transcript(empty, content=[])
    with pytest.raises(TranscriptError, match="non-empty"):
        ClipCandidateGenerator.load_transcript(empty)


@pytest.mark.parametrize(
    "segment",
    [
        {"id": 1, "start": -1, "end": 2, "text": "bad"},
        {"id": 1, "start": 2, "end": 1, "text": "bad"},
        {"id": 1, "start": "0", "end": 2, "text": "bad"},
        {"id": 1, "start": 0, "end": 2, "text": 5},
    ],
)
def test_rejects_malformed_segment_values(tmp_path, segment):
    path = tmp_path / "transcript.json"
    write_transcript(path, content=[segment])
    with pytest.raises(TranscriptError):
        ClipCandidateGenerator.load_transcript(path)


def test_missing_words_and_ids_are_allowed(tmp_path):
    path = tmp_path / "transcript.json"
    write_transcript(
        path,
        content=[{"start": 0, "end": 5, "text": "A complete statement."}],
    )
    loaded = ClipCandidateGenerator.load_transcript(path)
    assert loaded["segments"][0]["id"] == 0


def test_generation_combines_complete_segments_and_is_stable():
    generator = ClipCandidateGenerator.__new__(ClipCandidateGenerator)
    generator.configuration = CandidateConfiguration(
        minimum_duration_seconds=12,
        target_duration_seconds=18,
        maximum_duration_seconds=24,
        minimum_word_count=10,
        maximum_overlap=0.5,
        maximum_candidates=3,
    )
    transcript_segments = segments(8, 6)
    first = generator.generate_candidates("abc", transcript_segments)
    second = generator.generate_candidates("abc", transcript_segments)
    assert first == second
    assert first
    assert all(12 <= candidate["duration"] <= 24 for candidate in first)
    assert all(candidate["start"] % 6 == 0 for candidate in first)
    assert all(candidate["end"] % 6 == 0 for candidate in first)
    assert len(first) <= 3


def test_short_transcript_returns_no_candidates():
    generator = ClipCandidateGenerator.__new__(ClipCandidateGenerator)
    generator.configuration = CandidateConfiguration()
    assert generator.generate_candidates("abc", segments(2, 5)) == []


def test_scoring_is_deterministic_additive_and_penalizes_sponsors():
    good = (
        "Here is why you should use the best method. "
        "The reason is that it saves 3 hours. This is the clear conclusion."
    )
    sponsored = good + " Thanks to our sponsor; use code SAVE and subscribe."
    components, reasons = ClipCandidateGenerator.score_candidate(good, 30)
    again, _ = ClipCandidateGenerator.score_candidate(good, 30)
    bad_components, bad_reasons = ClipCandidateGenerator.score_candidate(
        sponsored, 30
    )
    assert components == again
    assert 0 <= max(0, min(100, sum(components.values()))) <= 100
    assert sum(bad_components.values()) < sum(components.values())
    assert any("recommendation" in reason for reason in reasons)
    assert any("complete statement" in reason for reason in reasons)
    assert any("sponsor" in reason for reason in bad_reasons)


def test_service_writes_atomic_artifact_and_updates_manifest(tmp_path):
    transcript = tmp_path / "input" / "transcript.json"
    write_transcript(transcript)
    manifest = VideoManifest(tmp_path / "videos.json")
    manifest.upsert(manifest_record("abc", transcript))
    output = tmp_path / "candidates"
    generator = ClipCandidateGenerator(
        manifest=manifest,
        output_directory=output,
        configuration=CandidateConfiguration(
            minimum_duration_seconds=12,
            target_duration_seconds=18,
            maximum_duration_seconds=24,
            minimum_word_count=10,
        ),
    )
    batch = generator.analyze()
    result = batch.results[0]
    assert result.status is AnalysisResultStatus.SUCCESS
    artifact = json.loads(Path(result.candidates_json_path).read_text())
    assert artifact["version"] == 1
    assert artifact["video_id"] == "abc"
    assert artifact["candidates"][0]["rank"] == 1
    assert manifest.get("abc")["clip_analysis"]["status"] == "completed"
    assert list(output.rglob("*.tmp")) == []


def test_service_skips_completed_unless_forced(tmp_path):
    transcript = tmp_path / "transcript.json"
    write_transcript(transcript)
    manifest = VideoManifest(tmp_path / "videos.json")
    manifest.upsert(manifest_record("abc", transcript))
    manifest.update_clip_analysis(
        "abc", status=ClipAnalysisStatus.COMPLETED.value
    )
    generator = ClipCandidateGenerator(manifest=manifest, output_directory=tmp_path)
    assert generator.analyze().skipped == 1
    assert generator.analyze(force=True).successful == 1


def test_service_marks_failure_and_continues(tmp_path):
    good_path = tmp_path / "good.json"
    write_transcript(good_path, "good")
    manifest = VideoManifest(tmp_path / "videos.json")
    manifest.upsert(manifest_record("bad", tmp_path / "missing.json"))
    manifest.upsert(manifest_record("good", good_path))
    generator = ClipCandidateGenerator(
        manifest=manifest,
        output_directory=tmp_path / "output",
        configuration=CandidateConfiguration(
            minimum_duration_seconds=12,
            target_duration_seconds=18,
            maximum_duration_seconds=24,
            minimum_word_count=10,
        ),
    )
    batch = generator.analyze()
    assert batch.failed == 1
    assert batch.successful == 1
    assert manifest.get("bad")["clip_analysis"]["status"] == "failed"
    assert "does not exist" in manifest.get("bad")["clip_analysis"]["error_message"]


def test_artifact_replace_failure_does_not_advertise_completion(tmp_path):
    transcript = tmp_path / "transcript.json"
    write_transcript(transcript)
    manifest = VideoManifest(tmp_path / "videos.json")
    manifest.upsert(manifest_record("abc", transcript))
    generator = ClipCandidateGenerator(manifest=manifest, output_directory=tmp_path / "out")
    original_replace = __import__("os").replace

    def fail_candidate(source, destination):
        if str(destination).endswith("candidates.json"):
            raise OSError("replace failed")
        return original_replace(source, destination)

    with patch(
        "backend.services.clip_candidate_generator.os.replace",
        side_effect=fail_candidate,
    ):
        batch = generator.analyze()
    assert batch.failed == 1
    assert manifest.get("abc")["clip_analysis"]["status"] == "failed"
    assert list((tmp_path / "out").rglob("*.tmp")) == []
