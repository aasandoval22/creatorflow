"""Command-line entry point for local vertical preview rendering."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from backend.services.video_manifest import DEFAULT_MANIFEST_PATH, ManifestError
from backend.services.video_preview_renderer import (
    DEFAULT_PREVIEW_DIRECTORY,
    SAFE_PRESETS,
    CaptionConfiguration,
    PreviewResultStatus,
    RenderConfiguration,
    VideoPreviewRenderer,
)


def positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def positive_number(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def even_integer(value: str) -> int:
    parsed = positive_integer(value)
    if parsed % 2:
        raise argparse.ArgumentTypeError("must be an even integer")
    return parsed


def crf_value(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if not 0 <= parsed <= 51:
        raise argparse.ArgumentTypeError("must be from 0 through 51")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render one transcript candidate as a local vertical MP4 preview."
    )
    parser.add_argument("--manifest-path", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--candidates-path", type=Path)
    parser.add_argument(
        "--output-directory", type=Path, default=DEFAULT_PREVIEW_DIRECTORY
    )
    parser.add_argument("--video-id", required=True)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--rank", type=positive_integer, default=None)
    selection.add_argument("--candidate-id")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-captions", action="store_true")
    parser.add_argument("--width", type=even_integer, default=1080)
    parser.add_argument("--height", type=even_integer, default=1920)
    parser.add_argument("--frame-rate", type=positive_number, default=30)
    parser.add_argument("--crf", type=crf_value, default=20)
    parser.add_argument("--preset", choices=SAFE_PRESETS, default="medium")
    parser.add_argument("--caption-font", default="DejaVu Sans")
    parser.add_argument("--caption-font-size", type=positive_integer, default=62)
    parser.add_argument("--caption-max-words", type=positive_integer, default=6)
    parser.add_argument("--caption-max-characters", type=positive_integer, default=34)
    parser.add_argument(
        "--caption-max-duration", type=positive_number, default=2.5
    )
    parser.add_argument("--ffmpeg-path", default="ffmpeg")
    parser.add_argument("--ffprobe-path", default="ffprobe")
    return parser


def main(argv: Sequence[str] | None = ()) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        renderer = VideoPreviewRenderer(
            manifest_path=args.manifest_path,
            output_directory=args.output_directory,
            configuration=RenderConfiguration(
                width=args.width,
                height=args.height,
                frame_rate=args.frame_rate,
                crf=args.crf,
                preset=args.preset,
                captions_enabled=not args.no_captions,
            ),
            caption_configuration=CaptionConfiguration(
                font_name=args.caption_font,
                font_size=args.caption_font_size,
                maximum_words=args.caption_max_words,
                maximum_characters=args.caption_max_characters,
                maximum_duration_seconds=args.caption_max_duration,
            ),
            ffmpeg_path=args.ffmpeg_path,
            ffprobe_path=args.ffprobe_path,
        )
        result = renderer.render(
            args.video_id,
            rank=args.rank,
            candidate_id=args.candidate_id,
            candidates_path=args.candidates_path,
            force=args.force,
            dry_run=args.dry_run,
        )
    except (ManifestError, OSError, ValueError) as error:
        print(f"Preview configuration failed: {error}")
        return 1

    if result.candidate_id:
        print(f"Selected candidate: {result.candidate_id} (rank {result.candidate_rank})")
        print(
            f"Source range: {result.start:.3f}-{result.end:.3f} "
            f"({result.duration:.3f}s)"
        )
    print(f"Output resolution: {args.width}x{args.height}")
    print(f"Captions enabled: {not args.no_captions}")
    if args.dry_run and result.command:
        print(f"FFmpeg command (display only): {renderer.display_command(result.command)}")
        print("Dry run: command was not rendered and no preview artifacts were created.")
    if result.output_path:
        print(f"Preview path: {result.output_path}")
    if result.metadata_path:
        print(f"Metadata path: {result.metadata_path}")
    print(f"Summary: {result.status.value} - {result.message}")
    return 0 if result.status in (
        PreviewResultStatus.SUCCESS, PreviewResultStatus.SKIPPED
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main(None))
