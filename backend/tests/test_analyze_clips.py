import json
import re

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


def test_cli_score_breakdown_is_opt_in(tmp_path, capsys):
    transcript = tmp_path / "transcript.json"
    write_transcript(transcript)
    manifest_path = tmp_path / "videos.json"
    manifest = VideoManifest(manifest_path)
    manifest.upsert(manifest_record("abc", transcript))
    base = [
        "--manifest-path", str(manifest_path),
        "--output-directory", str(tmp_path / "out"),
        "--video-id", "abc",
        "--minimum-duration", "12",
        "--target-duration", "18",
        "--maximum-duration", "24",
    ]
    assert main(base) == 0
    assert "Components:" not in capsys.readouterr().out
    assert main(base + ["--force", "--show-score-breakdown"]) == 0
    output = capsys.readouterr().out
    assert "Components:" in output
    assert "Ending classification:" in output
    assert "Boundaries:" in output
    assert "Component rounding tolerance: 0.1" in output
    assert "Positive reasons:" in output
    assert "Penalties:" in output


def test_cli_candidate_order_and_ranks_match_artifact(tmp_path, capsys):
    transcript = tmp_path / "transcript.json"
    write_transcript(transcript)
    manifest_path = tmp_path / "videos.json"
    manifest = VideoManifest(manifest_path)
    manifest.upsert(manifest_record("abc", transcript))
    output_directory = tmp_path / "out"
    assert main(
        [
            "--manifest-path", str(manifest_path),
            "--output-directory", str(output_directory),
            "--video-id", "abc",
            "--minimum-duration", "12",
            "--target-duration", "18",
            "--maximum-duration", "24",
        ]
    ) == 0
    output = capsys.readouterr().out
    displayed = [
        (int(rank), float(score))
        for rank, score in re.findall(r"#(\d+).*score (\d+\.\d)", output)
    ]
    artifact = json.loads(
        (output_directory / "abc" / "candidates.json").read_text()
    )
    serialized = [
        (candidate["rank"], candidate["score"])
        for candidate in artifact["candidates"]
    ]
    assert displayed == serialized
