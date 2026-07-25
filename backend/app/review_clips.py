"""Inspect and update the entirely local clip-review queue."""

from __future__ import annotations

import argparse
import html
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import quote

from backend.app.render_preview import positive_integer
from backend.services.clip_review_queue import (
    DEFAULT_REVIEW_QUEUE_PATH, REVIEW_STATUSES, ClipReviewQueue,
    ReviewQueueError,
)


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
    )
    print(f"Preview: {item['preview_path']}")
    print(f"Text: {text}")
    if complete:
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
                f"<p><b>Range:</b> {item['candidate_start']:.3f}–{item['candidate_end']:.3f} "
                f"({item['candidate_duration']:.3f}s)</p>"
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
        else:
            output = args.output_path or args.review_queue_path.with_name("index.html")
            print(f"Review index: {build_index(queue, output)}")
    except (OSError, ReviewQueueError, ValueError) as error:
        print(f"Review command failed: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(None))
