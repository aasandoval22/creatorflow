"""Inspect and update the entirely local clip-review queue."""

from __future__ import annotations

import argparse
import html
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import quote

from backend.app.render_preview import (
    crf_value, even_integer, positive_integer, positive_number,
)
from backend.services.clip_timing_adjustment import ClipTimingAdjustmentService
from backend.services.clip_review_queue import (
    DEFAULT_REVIEW_QUEUE_PATH, REVIEW_STATUSES, ClipReviewQueue,
    ReviewQueueError,
)
from backend.services.video_manifest import DEFAULT_MANIFEST_PATH
from backend.services.video_preview_renderer import (
    DEFAULT_PREVIEW_DIRECTORY, SAFE_PRESETS, CaptionConfiguration,
    RenderConfiguration, VideoPreviewRenderer,
)


def nonnegative_number(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return parsed


def _renderer_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest-path", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--output-directory", type=Path, default=DEFAULT_PREVIEW_DIRECTORY)
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
    parser.add_argument("--maximum-duration", type=positive_number, default=60)
    parser.add_argument("--allow-longer", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--note")
    parser.add_argument("--clear-note", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage the local clip-review queue.")
    parser.add_argument("--review-queue-path", type=Path, default=DEFAULT_REVIEW_QUEUE_PATH)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list")
    listing.add_argument("--status", choices=sorted(REVIEW_STATUSES))
    listing.add_argument("--video-id")
    listing.add_argument("--limit", type=positive_integer)
    show = commands.add_parser("show")
    show.add_argument("review_id")
    for name in ("approve", "reject"):
        action = commands.add_parser(name)
        action.add_argument("review_id")
        action.add_argument("--note")
    pending = commands.add_parser("pending")
    pending.add_argument("review_id")
    pending.add_argument("--note")
    pending.add_argument("--clear-note", action="store_true")
    index = commands.add_parser("build-index")
    index.add_argument("--output-path", type=Path)
    adjust = commands.add_parser("adjust")
    adjust.add_argument("review_id")
    adjust.add_argument("--lead-in", type=nonnegative_number)
    adjust.add_argument("--tail", type=nonnegative_number)
    adjust.add_argument("--render-start", type=nonnegative_number)
    adjust.add_argument("--render-end", type=positive_number)
    _renderer_arguments(adjust)
    reset = commands.add_parser("reset-timing")
    reset.add_argument("review_id")
    _renderer_arguments(reset)
    return parser


def _sorted(items: list[dict]) -> list[dict]:
    pending = sorted(
        (item for item in items if item["status"] == "pending"),
        key=lambda item: (-item["candidate_score"], item["created_at"]),
    )
    reviewed = sorted(
        (item for item in items if item["status"] != "pending"),
        key=lambda item: item["reviewed_at"] or "", reverse=True,
    )
    return pending + reviewed


def _print_item(item: dict, *, complete: bool = False) -> None:
    text = item["candidate_text"] if complete else (
        item["candidate_text"][:77] + "…" if len(item["candidate_text"]) > 78
        else item["candidate_text"]
    )
    print(
        f"{item['review_id']} | {item['status']} | {item['video_id']} | "
        f"rank {item['candidate_rank']} | score {item['candidate_score']:.1f} | "
        f"{item['candidate_duration']:.2f}s"
        + (
            f" | ADJUSTED {item['render_duration']:.2f}s"
            if item["timing_revision"] and (
                item["render_start"] != item["candidate_start"]
                or item["render_end"] != item["candidate_end"]
            ) else ""
        )
    )
    print(f"Preview: {item['preview_path']}")
    print(f"Text: {text}")
    if complete:
        print(
            f"Original candidate: {item['candidate_start']:.3f}-{item['candidate_end']:.3f} "
            f"({item['candidate_duration']:.3f}s)"
        )
        print(
            f"Current render: {item['render_start']:.3f}-{item['render_end']:.3f} "
            f"({item['render_duration']:.3f}s)"
        )
        print(f"Lead-in: {item['lead_in_seconds']:.3f}s")
        print(f"Tail: {item['tail_seconds']:.3f}s")
        print(f"Timing revision: {item['timing_revision']}")
        print(f"Timing updated: {item['timing_updated_at']}")
        for key, value in item.items():
            if key not in {"review_id", "status", "video_id", "candidate_rank",
                           "candidate_score", "candidate_duration", "preview_path",
                           "candidate_text"}:
                print(f"{key}: {value}")


def build_index(queue: ClipReviewQueue, output_path: Path) -> Path:
    items = queue.list_items()
    base = output_path.parent.resolve()
    sections = []
    for status in ("pending", "approved", "rejected"):
        cards = []
        for item in _sorted([entry for entry in items if entry["status"] == status]):
            path = Path(item["preview_path"])
            try:
                display_path = os.path.relpath(path.resolve(), base)
            except (OSError, ValueError):
                display_path = str(path)
            source = quote(display_path.replace(os.sep, "/"), safe="/:._-")
            cards.append(
                "<article>"
                f"<h3>Rank {item['candidate_rank']} · score {item['candidate_score']:.1f}</h3>"
                f"<video controls preload=\"metadata\" src=\"{html.escape(source, quote=True)}\"></video>"
                f"<p><b>Review ID:</b> {html.escape(item['review_id'])}</p>"
                f"<p><b>Video:</b> {html.escape(item['video_id'])}</p>"
                + ("<p><b>Timing adjusted</b></p>" if item["lead_in_seconds"] or item["tail_seconds"] else "")
                + f"<p><b>Original candidate:</b> {item['candidate_start']:.3f}–{item['candidate_end']:.3f} "
                f"({item['candidate_duration']:.3f}s)</p>"
                f"<p><b>Render range:</b> {item['render_start']:.3f}–{item['render_end']:.3f} "
                f"({item['render_duration']:.3f}s); lead-in {item['lead_in_seconds']:.3f}s; "
                f"tail {item['tail_seconds']:.3f}s</p>"
                f"<p>{html.escape(item['candidate_text'])}</p>"
                f"<p><b>Note:</b> {html.escape(item['review_note'] or '')}</p>"
                f"<p><b>Created:</b> {html.escape(item['created_at'])} "
                f"<b>Reviewed:</b> {html.escape(item['reviewed_at'] or '—')}</p>"
                "</article>"
            )
        sections.append(f"<section><h2>{status.title()}</h2>{''.join(cards) or '<p>No clips.</p>'}</section>")
    document = (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>CreatorFlow local review queue</title>"
        "<style>body{font-family:sans-serif;max-width:1000px;margin:auto;padding:1rem}"
        "article{border:1px solid #ccc;padding:1rem;margin:1rem 0}video{max-width:320px;width:100%}</style>"
        "</head><body><h1>CreatorFlow local review queue</h1>"
        + "".join(sections) + "</body></html>\n"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=output_path.parent,
            prefix=".review-index.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(document)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output_path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return output_path


def main(argv: Sequence[str] | None = ()) -> int:
    args = build_parser().parse_args(argv)
    try:
        queue = ClipReviewQueue(args.review_queue_path)
        if args.command == "list":
            items = _sorted(queue.list_items(status=args.status, video_id=args.video_id))
            if args.limit is not None:
                items = items[:args.limit]
            for item in items:
                _print_item(item)
            print(f"Reviews: {len(items)}")
        elif args.command == "show":
            item = queue.find_by_review_id(args.review_id)
            if item is None:
                raise ReviewQueueError(f"Review ID {args.review_id!r} was not found.")
            _print_item(item, complete=True)
        elif args.command == "approve":
            item = queue.approve(args.review_id, args.note)
            print(f"{item['review_id']}: {item['status']}")
        elif args.command == "reject":
            item = queue.reject(args.review_id, args.note)
            print(f"{item['review_id']}: {item['status']}")
        elif args.command == "pending":
            item = queue.return_to_pending(args.review_id, args.note, clear_note=args.clear_note)
            print(f"{item['review_id']}: {item['status']}")
        elif args.command == "build-index":
            output = args.output_path or args.review_queue_path.with_name("index.html")
            print(f"Review index: {build_index(queue, output)}")
        else:
            current = queue.find_by_review_id(args.review_id)
            if current is None:
                raise ReviewQueueError(f"Review ID {args.review_id!r} was not found.")
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
            service = ClipTimingAdjustmentService(
                queue, renderer, maximum_duration=args.maximum_duration
            )
            if args.command == "adjust":
                result = service.adjust(
                    args.review_id, lead_in=args.lead_in, tail=args.tail,
                    render_start=args.render_start, render_end=args.render_end,
                    allow_longer=args.allow_longer, note=args.note,
                    clear_note=args.clear_note, dry_run=args.dry_run,
                    force=True,
                )
            else:
                result = service.reset(
                    args.review_id, allow_longer=args.allow_longer,
                    note=args.note, clear_note=args.clear_note,
                    dry_run=args.dry_run, force=True,
                )
            print(
                f"Original candidate: {current['candidate_start']:.3f}-"
                f"{current['candidate_end']:.3f} ({current['candidate_duration']:.3f}s)"
            )
            print(
                f"Previous render: {current['render_start']:.3f}-"
                f"{current['render_end']:.3f} ({current['render_duration']:.3f}s)"
            )
            print(
                f"Proposed render: {result.render_start:.3f}-{result.render_end:.3f} "
                f"({result.render_end - result.render_start:.3f}s)"
            )
            print(
                f"Lead-in: {current['candidate_start'] - result.render_start:.3f}s; "
                f"tail: {result.render_end - current['candidate_end']:.3f}s"
            )
            if args.dry_run and result.preview.command:
                print(f"FFmpeg command (display only): {renderer.display_command(result.preview.command)}")
            print(f"Preview path: {result.preview.output_path}")
            print(f"Metadata path: {result.preview.metadata_path}")
            print(f"Timing revision: {current['timing_revision'] + 1}")
    except (OSError, ReviewQueueError, ValueError) as error:
        print(f"Review command failed: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(None))
