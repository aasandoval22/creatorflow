"""Capture, run, and inspect immutable local review-comparison batches."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from backend.services.clip_review_queue import DEFAULT_REVIEW_QUEUE_PATH, ClipReviewQueue
from backend.services.reference_annotations import DEFAULT_ANNOTATION_ROOT, ReferenceAnnotationStore
from backend.services.reference_clip_comparator import ReferenceClipComparator
from backend.services.reference_clip_library import (
    DEFAULT_REFERENCE_INDEX, DEFAULT_REFERENCE_ROOT, ReferenceClipLibrary,
)
from backend.services.reference_profile_builder import DEFAULT_PROFILE_ROOT, ReferenceProfileBuilder
from backend.services.review_comparison_batches import (
    DEFAULT_BATCH_ROOT, ReviewComparisonBatchError, ReviewComparisonBatchService,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage stable local review-comparison batches.")
    parser.add_argument("--review-queue-path", type=Path, default=DEFAULT_REVIEW_QUEUE_PATH)
    parser.add_argument("--reference-root", type=Path, default=DEFAULT_REFERENCE_ROOT)
    parser.add_argument("--index-path", type=Path, default=DEFAULT_REFERENCE_INDEX)
    parser.add_argument("--profile-directory", type=Path, default=DEFAULT_PROFILE_ROOT)
    parser.add_argument("--annotation-directory", type=Path, default=DEFAULT_ANNOTATION_ROOT)
    parser.add_argument("--batch-directory", type=Path, default=DEFAULT_BATCH_ROOT)
    sub = parser.add_subparsers(dest="command", required=True)
    capture = sub.add_parser("capture")
    capture.add_argument("--profile", required=True)
    for command in ("run", "show"):
        child = sub.add_parser(command)
        child.add_argument("--batch-id", required=True)
    return parser


def main(argv: Sequence[str] | None = ()) -> int:
    args = build_parser().parse_args(argv)
    library = ReferenceClipLibrary(args.reference_root, args.index_path)
    builder = ReferenceProfileBuilder(
        library, args.profile_directory,
        annotation_store=ReferenceAnnotationStore(args.annotation_directory),
    )
    queue = ClipReviewQueue(args.review_queue_path)
    service = ReviewComparisonBatchService(
        queue, builder, ReferenceClipComparator(builder), root=args.batch_directory,
    )
    try:
        if args.command == "capture":
            result = service.capture(args.profile)
            print(f"Captured batch: {result['batch_id']}")
            print(f"Items: {len(result['items'])}")
            print(f"Profile SHA-256: {result['profile_sha256']}")
        elif args.command == "run":
            result = service.run(args.batch_id)
            print(f"Batch {args.batch_id}: {result['status']} ({result['item_count']} reports)")
        else:
            print(json.dumps(service.show(args.batch_id), indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, ReviewComparisonBatchError) as error:
        print(f"review-comparisons: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(None))
