from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backend.app.transcribe_videos import build_parser, main
from backend.services.video_manifest import DEFAULT_MANIFEST_PATH
from backend.services.video_transcriber import (
    DEFAULT_TRANSCRIPT_DIRECTORY,
    TranscriptionBatchResult,
    TranscriptionResult,
    TranscriptionResultStatus,
)


def test_default_and_custom_arguments():
    defaults = build_parser().parse_args([])
    custom = build_parser().parse_args(
        [
            "--manifest-path",
            "custom.json",
            "--output-directory",
            "output",
            "--model",
            "small.en",
            "--device",
            "cuda",
            "--compute-type",
            "float16",
            "--language",
            "fr",
            "--limit",
            "2",
            "--video-id",
            "abc",
            "--retry-failed",
            "--force",
        ]
    )

    assert defaults.manifest_path == DEFAULT_MANIFEST_PATH
    assert defaults.output_directory == DEFAULT_TRANSCRIPT_DIRECTORY
    assert (defaults.model, defaults.device, defaults.compute_type) == (
        "base.en",
        "cpu",
        "int8",
    )
    assert defaults.language == "en"
    assert custom.manifest_path == Path("custom.json")
    assert custom.output_directory == Path("output")
    assert (
        custom.model,
        custom.device,
        custom.compute_type,
        custom.language,
    ) == ("small.en", "cuda", "float16", "fr")
    assert custom.limit == 2
    assert custom.retry_failed and custom.force


@pytest.mark.parametrize("value", ["0", "-1", "nope"])
def test_limit_rejects_invalid_values(value):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--limit", value])


def test_main_passes_selection_flags_and_prints_summary(capsys):
    instance = MagicMock()
    instance.manifest.get.return_value = {"video_id": "abc"}
    instance.transcribe.return_value = TranscriptionBatchResult(
        [
            TranscriptionResult(
                "abc", TranscriptionResultStatus.SUCCESS, "done"
            ),
            TranscriptionResult(
                "def", TranscriptionResultStatus.SKIPPED, "already done"
            ),
        ]
    )
    with patch(
        "backend.app.transcribe_videos.VideoTranscriber",
        return_value=instance,
    ) as transcriber_class:
        exit_code = main(
            [
                "--manifest-path",
                "manifest.json",
                "--output-directory",
                "out",
                "--video-id",
                "abc",
                "--limit",
                "1",
                "--retry-failed",
                "--force",
            ]
        )

    assert exit_code == 0
    assert transcriber_class.call_args.kwargs["manifest_path"] == Path(
        "manifest.json"
    )
    assert transcriber_class.call_args.kwargs["output_directory"] == Path(
        "out"
    )
    instance.transcribe.assert_called_once_with(
        limit=1, video_id="abc", retry_failed=True, force=True
    )
    output = capsys.readouterr().out
    assert "abc: success - done" in output
    assert "Summary: 1 successful, 1 skipped, 0 failed." in output


def test_main_failure_and_missing_video_exit_nonzero(capsys):
    instance = MagicMock()
    instance.manifest.get.return_value = None
    with patch(
        "backend.app.transcribe_videos.VideoTranscriber",
        return_value=instance,
    ):
        assert main(["--video-id", "missing"]) == 1
    assert "was not found" in capsys.readouterr().out

    instance.manifest.get.return_value = {"video_id": "abc"}
    instance.transcribe.return_value = TranscriptionBatchResult(
        [
            TranscriptionResult(
                "abc", TranscriptionResultStatus.FAILED, "decode failed"
            )
        ]
    )
    with patch(
        "backend.app.transcribe_videos.VideoTranscriber",
        return_value=instance,
    ):
        assert main([]) == 1
    assert "Summary: 0 successful, 0 skipped, 1 failed." in (
        capsys.readouterr().out
    )


def test_missing_faster_whisper_message_is_actionable(tmp_path, capsys):
    from backend.services.video_manifest import VideoManifest
    from backend.tests.test_video_transcriber import video_record

    media = tmp_path / "media.mp4"
    media.write_bytes(b"fake")
    manifest_path = tmp_path / "manifest.json"
    VideoManifest(manifest_path).upsert(video_record("abc", media))

    with patch.dict("sys.modules", {"faster_whisper": None}):
        exit_code = main(
            [
                "--manifest-path",
                str(manifest_path),
                "--output-directory",
                str(tmp_path / "out"),
            ]
        )

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "faster-whisper is not installed" in output
    assert "requirements-transcription.txt" in output
