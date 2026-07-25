import json
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.services.video_manifest import (
    ClipAnalysisStatus,
    ManifestError,
    TranscriptionStatus,
    VideoManifest,
    VideoStatus,
    default_clip_analysis,
    default_transcription,
)


def record(video_id="abc", status=VideoStatus.DISCOVERED.value):
    return {
        "video_id": video_id,
        "source_platform": "youtube",
        "channel_name": "Creator",
        "channel_url": "https://www.youtube.com/@creator",
        "video_url": f"https://www.youtube.com/watch?v={video_id}",
        "title": "A video",
        "uploader": "Creator",
        "upload_date": "20260724",
        "duration_seconds": 42,
        "discovered_at": "2026-07-24T12:00:00+00:00",
        "downloaded_at": None,
        "local_file_path": None,
        "status": status,
        "error_message": None,
        "transcription": default_transcription(),
        "clip_analysis": default_clip_analysis(),
    }


def test_creates_empty_manifest_and_parent_directory(tmp_path):
    path = tmp_path / "nested" / "videos.json"

    manifest = VideoManifest(path)

    assert path.exists()
    assert manifest.read_records() == []
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "version": 3,
        "videos": [],
    }


def test_reads_and_writes_records(tmp_path):
    manifest = VideoManifest(tmp_path / "videos.json")

    saved = manifest.upsert(record())

    assert saved == record()
    assert manifest.get("abc") == record()


def test_updates_without_duplication_and_preserves_discovered_at(tmp_path):
    manifest = VideoManifest(tmp_path / "videos.json")
    manifest.upsert(record())
    updated = record(status=VideoStatus.DOWNLOADED.value)
    updated["discovered_at"] = "2026-07-25T12:00:00+00:00"
    updated["downloaded_at"] = "2026-07-25T12:01:00+00:00"

    saved = manifest.upsert(updated)

    assert len(manifest.read_records()) == 1
    assert saved["status"] == "downloaded"
    assert saved["discovered_at"] == "2026-07-24T12:00:00+00:00"
    assert saved["downloaded_at"] == "2026-07-25T12:01:00+00:00"


@pytest.mark.parametrize("status", [status.value for status in VideoStatus])
def test_accepts_all_supported_statuses(tmp_path, status):
    manifest = VideoManifest(tmp_path / f"{status}.json")

    manifest.upsert(record(status=status))

    assert manifest.get("abc")["status"] == status


def test_atomic_write_replaces_manifest_and_removes_temporary_file(tmp_path):
    path = tmp_path / "videos.json"
    manifest = VideoManifest(path)

    manifest.upsert(record())

    assert manifest.get("abc") == record()
    assert list(tmp_path.glob(".videos.json.*.tmp")) == []


def test_atomic_failure_preserves_previous_manifest_and_cleans_temp(tmp_path):
    path = tmp_path / "videos.json"
    manifest = VideoManifest(path)
    original = path.read_text(encoding="utf-8")

    with patch(
        "backend.services.video_manifest.os.replace",
        side_effect=OSError("replace failed"),
    ):
        with pytest.raises(OSError, match="replace failed"):
            manifest.upsert(record())

    assert path.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob(".videos.json.*.tmp")) == []


def test_invalid_json_has_actionable_error(tmp_path):
    path = tmp_path / "videos.json"
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(ManifestError, match="Invalid JSON.*Repair or remove"):
        VideoManifest(path)


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ([], "must be a JSON object"),
        ({"videos": []}, "exactly 'version' and 'videos'"),
        ({"version": 1, "videos": {}}, "'videos' must be a list"),
        ({"version": 4, "videos": []}, "unsupported version"),
    ],
)
def test_rejects_incorrect_top_level_structure(
    tmp_path, document, message
):
    path = tmp_path / "videos.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ManifestError, match=message):
        VideoManifest(path)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("not an object", "must be a JSON object"),
        ({"video_id": "abc"}, "is malformed"),
        ({**record(), "duration_seconds": -1}, "non-negative number"),
        ({**record(), "discovered_at": "yesterday"}, "ISO 8601"),
        (
            {**record(), "discovered_at": "2026-07-24T12:00:00"},
            "must use UTC",
        ),
    ],
)
def test_rejects_malformed_records(tmp_path, value, message):
    path = tmp_path / "videos.json"
    path.write_text(
        json.dumps({"version": 3, "videos": [value]}), encoding="utf-8"
    )

    with pytest.raises(ManifestError, match=message):
        VideoManifest(path)


def test_rejects_invalid_status(tmp_path):
    path = tmp_path / "videos.json"
    path.write_text(
        json.dumps(
            {"version": 3, "videos": [record(status="transcribing")]}
        ),
        encoding="utf-8",
    )

    with pytest.raises(ManifestError, match="invalid status"):
        VideoManifest(path)


def test_migrates_version_one_atomically_and_preserves_records(tmp_path):
    path = tmp_path / "videos.json"
    old_record = record()
    old_record.pop("transcription")
    old_record.pop("clip_analysis")
    path.write_text(
        json.dumps({"version": 1, "videos": [old_record]}),
        encoding="utf-8",
    )

    manifest = VideoManifest(path)

    saved = manifest.get("abc")
    assert saved | {} == {
        **old_record,
        "transcription": default_transcription(),
        "clip_analysis": default_clip_analysis(),
    }
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 3
    assert list(tmp_path.glob(".videos.json.*.tmp")) == []
    assert VideoManifest(path).get("abc") == saved


def test_migration_failure_preserves_version_one_file(tmp_path):
    path = tmp_path / "videos.json"
    old_record = record()
    old_record.pop("transcription")
    old_record.pop("clip_analysis")
    original = json.dumps({"version": 1, "videos": [old_record]})
    path.write_text(original, encoding="utf-8")

    with patch(
        "backend.services.video_manifest.os.replace",
        side_effect=OSError("replace failed"),
    ):
        with pytest.raises(OSError, match="replace failed"):
            VideoManifest(path)

    assert path.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob(".videos.json.*.tmp")) == []


def test_migrates_version_two_to_three_preserving_transcription(tmp_path):
    path = tmp_path / "videos.json"
    old_record = record()
    old_record.pop("clip_analysis")
    old_record["transcription"].update(
        status=TranscriptionStatus.COMPLETED.value,
        transcript_json_path="/tmp/transcript.json",
    )
    path.write_text(
        json.dumps({"version": 2, "videos": [old_record]}),
        encoding="utf-8",
    )

    saved = VideoManifest(path).get("abc")

    assert saved["transcription"] == old_record["transcription"]
    assert saved["clip_analysis"] == default_clip_analysis()
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 3


@pytest.mark.parametrize(
    ("analysis", "message"),
    [
        (None, "must be an object"),
        ({}, "structure is malformed"),
        (
            {**default_clip_analysis(), "status": "unknown"},
            "invalid clip-analysis status",
        ),
        (
            {**default_clip_analysis(), "candidate_count": -1},
            "non-negative integer",
        ),
        (
            {**default_clip_analysis(), "candidate_count": True},
            "non-negative integer",
        ),
        (
            {**default_clip_analysis(), "candidates_json_path": 5},
            "string or null",
        ),
        (
            {**default_clip_analysis(), "started_at": "yesterday"},
            "ISO 8601",
        ),
    ],
)
def test_rejects_invalid_clip_analysis(tmp_path, analysis, message):
    path = tmp_path / "videos.json"
    path.write_text(
        json.dumps(
            {
                "version": 3,
                "videos": [{**record(), "clip_analysis": analysis}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ManifestError, match=message):
        VideoManifest(path)


def test_clip_analysis_update_and_rediscovery_preserve_state(tmp_path):
    manifest = VideoManifest(tmp_path / "videos.json")
    original = manifest.upsert(record())
    manifest.update_clip_analysis(
        "abc",
        status=ClipAnalysisStatus.COMPLETED.value,
        started_at="2026-07-24T12:01:00+00:00",
        completed_at="2026-07-24T12:02:00+00:00",
        candidate_count=2,
        candidates_json_path="/tmp/candidates.json",
    )
    rediscovered = record()
    rediscovered["title"] = "Updated"
    saved = manifest.upsert(rediscovered)
    assert saved["clip_analysis"]["status"] == "completed"
    assert saved["clip_analysis"]["candidate_count"] == 2
    assert saved["title"] == "Updated"
    assert saved["video_id"] == original["video_id"]


@pytest.mark.parametrize(
    ("transcription", "message"),
    [
        (None, "must be an object"),
        ({}, "structure is malformed"),
        (
            {**default_transcription(), "status": "unknown"},
            "invalid transcription status",
        ),
        (
            {**default_transcription(), "transcript_json_path": 5},
            "must be a string or null",
        ),
        (
            {**default_transcription(), "started_at": "yesterday"},
            "ISO 8601",
        ),
        (
            {
                **default_transcription(),
                "completed_at": "2026-07-24T12:00:00",
            },
            "must use UTC",
        ),
    ],
)
def test_rejects_invalid_transcription(tmp_path, transcription, message):
    path = tmp_path / "videos.json"
    path.write_text(
        json.dumps(
            {
                "version": 3,
                "videos": [{**record(), "transcription": transcription}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ManifestError, match=message):
        VideoManifest(path)


def test_rediscovery_preserves_completed_transcription(tmp_path):
    manifest = VideoManifest(tmp_path / "videos.json")
    manifest.upsert(record())
    manifest.update_transcription(
        "abc",
        status=TranscriptionStatus.COMPLETED.value,
        model="base.en",
        language="en",
        started_at="2026-07-24T12:01:00+00:00",
        completed_at="2026-07-24T12:02:00+00:00",
        transcript_json_path="/tmp/transcript.json",
        transcript_text_path="/tmp/transcript.txt",
        subtitle_srt_path="/tmp/subtitles.srt",
    )
    rediscovered = record()
    rediscovered["title"] = "Updated title"

    saved = manifest.upsert(rediscovered)

    assert saved["title"] == "Updated title"
    assert saved["transcription"]["status"] == "completed"
    assert saved["transcription"]["transcript_json_path"].endswith(
        "transcript.json"
    )


def test_transcription_update_preserves_video_metadata(tmp_path):
    manifest = VideoManifest(tmp_path / "videos.json")
    original = manifest.upsert(record())

    updated = manifest.update_transcription(
        "abc",
        status=TranscriptionStatus.PROCESSING.value,
        started_at="2026-07-24T12:01:00+00:00",
    )

    assert {key: updated[key] for key in original if key != "transcription"} == {
        key: original[key] for key in original if key != "transcription"
    }
    assert updated["transcription"]["status"] == "processing"
