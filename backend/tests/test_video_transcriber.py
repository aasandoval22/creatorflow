import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from backend.services.video_manifest import (
    TranscriptionStatus,
    VideoManifest,
    VideoStatus,
    default_transcription,
)
from backend.services.video_transcriber import (
    TranscriptionResultStatus,
    VideoTranscriber,
)


def video_record(video_id, media_path, status=VideoStatus.DOWNLOADED.value):
    return {
        "video_id": video_id,
        "source_platform": "youtube",
        "channel_name": "Creator",
        "channel_url": None,
        "video_url": f"https://youtube.test/watch?v={video_id}",
        "title": f"Video {video_id}",
        "uploader": "Creator",
        "upload_date": "20260724",
        "duration_seconds": 4.2,
        "discovered_at": "2026-07-24T12:00:00+00:00",
        "downloaded_at": "2026-07-24T12:01:00+00:00",
        "local_file_path": str(media_path) if media_path is not None else None,
        "status": status,
        "error_message": None,
        "transcription": default_transcription(),
    }


def fake_output(events=None):
    word = SimpleNamespace(
        start=0.1, end=0.5, word=" Hello", probability=0.98
    )
    segments = [
        SimpleNamespace(
            id=0, start=0.0, end=4.2, text=" Hello world ", words=[word]
        ),
        SimpleNamespace(
            id=1,
            start=4.2,
            end=5.0,
            text=" Again",
            words=[
                SimpleNamespace(
                    start=None, end=None, word=" Again", probability=None
                )
            ],
        ),
    ]

    def lazy_segments():
        for segment in segments:
            if events is not None:
                events.append("segment")
            yield segment

    info = SimpleNamespace(
        language="en", language_probability=0.99, duration=5.0
    )
    return lazy_segments(), info


@pytest.fixture
def setup_transcriber(tmp_path):
    manifest = VideoManifest(tmp_path / "manifest.json")
    media = tmp_path / "media.mp4"
    media.write_bytes(b"fake")
    manifest.upsert(video_record("abc", media))
    model = MagicMock()
    model.transcribe.return_value = fake_output()
    transcriber = VideoTranscriber(
        manifest=manifest,
        output_directory=tmp_path / "transcripts",
        model=model,
    )
    return transcriber, manifest, model, media


def test_transcribes_with_required_settings_and_writes_artifacts(
    setup_transcriber,
):
    transcriber, manifest, model, media = setup_transcriber

    batch = transcriber.transcribe()

    assert batch.successful == 1
    model.transcribe.assert_called_once_with(
        str(media),
        word_timestamps=True,
        vad_filter=True,
        language="en",
        beam_size=5,
    )
    state = manifest.get("abc")["transcription"]
    assert state["status"] == "completed"
    assert state["started_at"].endswith("+00:00")
    assert state["completed_at"].endswith("+00:00")
    document = json.loads(
        (transcriber.output_directory / "abc" / "transcript.json").read_text()
    )
    assert document["version"] == 1
    assert document["source_media_path"] == media.relative_to("/tmp").as_posix()
    assert manifest.paths.resolve(document["source_media_path"]) == media
    assert document["text"] == "Hello world Again"
    assert document["segments"][0]["words"][0]["probability"] == 0.98
    assert document["segments"][1]["words"][0]["start"] is None
    assert (
        transcriber.output_directory / "abc" / "transcript.txt"
    ).read_text() == "Hello world Again\n"
    srt = (
        transcriber.output_directory / "abc" / "subtitles.srt"
    ).read_text()
    assert "1\n00:00:00,000 --> 00:00:04,200\nHello world" in srt
    assert "2\n00:00:04,200 --> 00:00:05,000\nAgain" in srt
    assert list(transcriber.output_directory.rglob("*.tmp")) == []


def test_fully_consumes_lazy_segments_and_processing_precedes_execution(
    setup_transcriber,
):
    transcriber, manifest, model, _ = setup_transcriber
    events = []
    model.transcribe.return_value = fake_output(events)
    original_update = manifest.update_transcription

    def tracked_update(video_id, **changes):
        events.append(changes["status"])
        return original_update(video_id, **changes)

    manifest.update_transcription = tracked_update

    transcriber.transcribe()

    assert events[0] == "processing"
    assert events.count("segment") == 2
    assert events[-1] == "completed"


@pytest.mark.parametrize(
    ("path_value", "message"),
    [(None, "path is missing"), ("missing.mp4", "does not exist")],
)
def test_missing_media_marks_failed(tmp_path, path_value, message):
    manifest = VideoManifest(tmp_path / "manifest.json")
    manifest.upsert(video_record("abc", path_value))
    transcriber = VideoTranscriber(
        manifest=manifest,
        output_directory=tmp_path / "out",
        model=MagicMock(),
    )

    batch = transcriber.transcribe()

    assert batch.failed == 1
    assert message in batch.results[0].message
    assert manifest.get("abc")["transcription"]["status"] == "failed"


def test_model_failure_marks_failed_and_continues(tmp_path):
    manifest = VideoManifest(tmp_path / "manifest.json")
    model = MagicMock()
    for video_id in ("bad", "good"):
        media = tmp_path / f"{video_id}.mp4"
        media.write_bytes(b"fake")
        manifest.upsert(video_record(video_id, media))
    model.transcribe.side_effect = [RuntimeError("decode failed"), fake_output()]
    transcriber = VideoTranscriber(
        manifest=manifest, output_directory=tmp_path / "out", model=model
    )

    batch = transcriber.transcribe()

    assert [result.status for result in batch.results] == [
        TranscriptionResultStatus.FAILED,
        TranscriptionResultStatus.SUCCESS,
    ]
    assert "decode failed" in manifest.get("bad")["transcription"][
        "error_message"
    ]
    assert manifest.get("good")["transcription"]["status"] == "completed"


def test_artifact_failure_marks_failed_and_cleans_temporary_files(
    setup_transcriber,
):
    transcriber, manifest, _, _ = setup_transcriber
    real_replace = __import__("os").replace
    calls = 0

    def fail_artifact(source, destination):
        nonlocal calls
        if str(destination).endswith("transcript.json"):
            calls += 1
            raise OSError("disk full")
        return real_replace(source, destination)

    with patch(
        "backend.services.video_transcriber.os.replace",
        side_effect=fail_artifact,
    ):
        batch = transcriber.transcribe()

    assert calls == 1
    assert batch.failed == 1
    assert manifest.get("abc")["transcription"]["status"] == "failed"
    assert list(transcriber.output_directory.rglob("*.tmp")) == []


def test_selection_limit_statuses_retry_force_and_video_id(tmp_path):
    manifest = VideoManifest(tmp_path / "manifest.json")
    model = MagicMock()
    model.transcribe.side_effect = lambda *_args, **_kwargs: fake_output()
    for video_id in ("one", "two", "complete", "retry"):
        media = tmp_path / f"{video_id}.mp4"
        media.write_bytes(b"fake")
        manifest.upsert(video_record(video_id, media))
    for video_id, state in (
        ("complete", TranscriptionStatus.COMPLETED.value),
        ("retry", TranscriptionStatus.FAILED.value),
    ):
        manifest.update_transcription(video_id, status=state)
    for status in (
        VideoStatus.DISCOVERED,
        VideoStatus.SKIPPED,
        VideoStatus.FAILED,
    ):
        manifest.upsert(video_record(status.value, None, status.value))
    transcriber = VideoTranscriber(
        manifest=manifest, output_directory=tmp_path / "out", model=model
    )

    limited = transcriber.transcribe(limit=1)
    completed_skip = transcriber.transcribe(video_id="complete")
    retry = transcriber.transcribe(video_id="retry", retry_failed=True)
    forced = transcriber.transcribe(video_id="complete", force=True)

    assert [result.video_id for result in limited.results] == ["one"]
    assert completed_skip.skipped == 1
    assert retry.successful == 1
    assert forced.successful == 1
    assert model.transcribe.call_count == 3


def test_injected_factory_receives_production_configuration(tmp_path):
    manifest = VideoManifest(tmp_path / "manifest.json")
    media = tmp_path / "media.mp4"
    media.write_bytes(b"fake")
    manifest.upsert(video_record("abc", media))
    model = MagicMock()
    model.transcribe.return_value = fake_output()
    factory = MagicMock(return_value=model)
    transcriber = VideoTranscriber(
        manifest=manifest,
        output_directory=tmp_path / "out",
        model_factory=factory,
        model_name="tiny.en",
        device="cuda",
        compute_type="float16",
        language="fr",
    )

    transcriber.transcribe()

    factory.assert_called_once_with(
        "tiny.en", device="cuda", compute_type="float16"
    )
    assert model.transcribe.call_args.kwargs["language"] == "fr"


def test_empty_segments_and_missing_info_metadata_are_safe(tmp_path):
    manifest = VideoManifest(tmp_path / "manifest.json")
    media = tmp_path / "media.mp4"
    media.write_bytes(b"fake")
    manifest.upsert(video_record("abc", media))
    model = MagicMock()
    model.transcribe.return_value = (iter([]), SimpleNamespace())
    transcriber = VideoTranscriber(
        manifest=manifest, output_directory=tmp_path / "out", model=model
    )

    batch = transcriber.transcribe()

    document = json.loads(
        (tmp_path / "out" / "abc" / "transcript.json").read_text()
    )
    assert batch.successful == 1
    assert document["language_probability"] is None
    assert document["duration_seconds"] is None
    assert document["text"] == ""
    assert (tmp_path / "out" / "abc" / "subtitles.srt").read_text() == ""
