import json

import pytest

from backend.app.analyze_clips import build_parser, main
from backend.tests.test_clip_candidate_generator import (
    manifest_record,
    write_transcript,
)
from backend.services.video_manifest import VideoManifest


def test_parser_defaults_and_custom_values():
    defaults = build_parser().parse_args([])
    assert defaults.minimum_duration == 20
    assert defaults.target_duration == 35
    assert defaults.maximum_duration == 60
    custom = build_parser().parse_args(
        ["--minimum-duration", "10", "--target-duration", "20", "--maximum-duration", "30"]
    )
    assert custom.target_duration == 20


@pytest.mark.parametrize("args", [["--limit", "0"], ["--maximum-candidates", "-1"], ["--minimum-duration", "0"]])
def test_parser_rejects_invalid_numeric_values(args):
    with pytest.raises(SystemExit):
        build_parser().parse_args(args)


def test_cli_rejects_invalid_duration_relationship(capsys):
    assert main(["--minimum-duration", "40", "--target-duration", "20"]) == 1
    assert "ordered" in capsys.readouterr().out


def test_cli_missing_video_id_returns_nonzero(tmp_path, capsys):
    path = tmp_path / "videos.json"
    VideoManifest(path)
    assert main(["--manifest-path", str(path), "--video-id", "missing"]) == 1
    assert "was not found" in capsys.readouterr().out


def test_cli_prints_candidates_and_summary(tmp_path, capsys):
    transcript = tmp_path / "transcript.json"
    write_transcript(transcript)
    manifest_path = tmp_path / "videos.json"
    manifest = VideoManifest(manifest_path)
    manifest.upsert(manifest_record("abc", transcript))
    code = main(
        [
            "--manifest-path", str(manifest_path),
            "--output-directory", str(tmp_path / "out"),
            "--video-id", "abc",
            "--minimum-duration", "12",
            "--target-duration", "18",
            "--maximum-duration", "24",
        ]
    )
    output = capsys.readouterr().out
    assert code == 0
    assert "#1" in output
    assert "Summary: 1 successful, 0 skipped, 0 failed." in output
    artifact = json.loads((tmp_path / "out" / "abc" / "candidates.json").read_text())
    assert artifact["candidates"]
