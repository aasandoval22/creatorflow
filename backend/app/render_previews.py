"""Render several ranked candidates into the local review queue."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from backend.app.render_preview import (
    crf_value, even_integer, positive_integer, positive_number,
)
from backend.services.batch_preview_renderer import BatchPreviewRenderer
from backend.services.clip_review_queue import (
    DEFAULT_REVIEW_QUEUE_PATH, ClipReviewQueue,
)
from backend.services.video_manifest import DEFAULT_MANIFEST_PATH
from backend.services.video_preview_renderer import (
    DEFAULT_PREVIEW_DIRECTORY, SAFE_PRESETS, CaptionConfiguration,
    RenderConfiguration, VideoPreviewRenderer,
)


def ranks_value(value: str) -> list[int]:
    if not value or any(not part.strip() for part in value.split(",")):
        raise argparse.ArgumentTypeError("must be a comma-separated list of positive integers")
    try:
        ranks = [int(part.strip()) for part in value.split(",")]
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a comma-separated list of positive integers") from error
    if any(rank < 1 for rank in ranks):
        raise argparse.ArgumentTypeError("ranks must be positive")
    if len(set(ranks)) != len(ranks):
        raise argparse.ArgumentTypeError("ranks must not contain duplicates")
    return ranks


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render ranked previews into a local review queue.")
    parser.add_argument("--manifest-path", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--candidates-path", type=Path)
    parser.add_argument("--output-directory", type=Path, default=DEFAULT_PREVIEW_DIRECTORY)
    parser.add_argument("--review-queue-path", type=Path, default=DEFAULT_REVIEW_QUEUE_PATH)
    parser.add_argument("--video-id", required=True)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--top", type=positive_integer, default=3)
    selection.add_argument("--ranks", type=ranks_value)
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
    parser.add_argument("--caption-max-duration", type=positive_number, default=2.5)
    parser.add_argument("--ffmpeg-path", default="ffmpeg")
    parser.add_argument("--ffprobe-path", default="ffprobe")
    return parser


def main(argv: Sequence[str] | None = ()) -> int:
    args = build_parser().parse_args(argv)
    try:
        renderer = VideoPreviewRenderer(
            manifest_path=args.manifest_path,
            output_directory=args.output_directory,
            configuration=RenderConfiguration(
                width=args.width, height=args.height, frame_rate=args.frame_rate,
                crf=args.crf, preset=args.preset,
                captions_enabled=not args.no_captions,
            ),
            caption_configuration=CaptionConfiguration(
                font_name=args.caption_font, font_size=args.caption_font_size,
                maximum_words=args.caption_max_words,
                maximum_characters=args.caption_max_characters,
                maximum_duration_seconds=args.caption_max_duration,
            ),
            ffmpeg_path=args.ffmpeg_path, ffprobe_path=args.ffprobe_path,
        )
        batch = BatchPreviewRenderer(renderer, ClipReviewQueue(args.review_queue_path))
        result = batch.render(
            args.video_id, top=args.top, ranks=args.ranks,
            candidates_path=args.candidates_path, force=args.force,
            dry_run=args.dry_run,
        )
    except (OSError, ValueError) as error:
        print(f"Batch preview configuration failed: {error}")
        return 1
    for item in result.items:
        print(f"Selected candidate: {item.candidate_id or '(missing)'} (rank {item.rank})")
        print(f"Result: {item.status} - {item.message}")
        if item.preview_path:
            print(f"Preview path: {item.preview_path}")
        if item.review_id:
            print(f"Review ID: {item.review_id}")
    print(
        f"Summary: successful={result.successful}, skipped={result.skipped}, "
        f"failed={result.failed}"
    )
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main(None))
