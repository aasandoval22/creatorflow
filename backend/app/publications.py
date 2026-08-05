"""Local publication inspection and bounded status reconciliation."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import TextIO

from backend.services.clip_review_queue import ClipReviewQueue
from backend.services.publication import PublicationError
from backend.services.tiktok import TikTokPublicationService
from backend.services.video_manifest import VideoManifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect publication records or reconcile TikTok status."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    reconcile = commands.add_parser("reconcile")
    reconcile.add_argument("--limit", type=int, default=1)
    return parser


def main(
    argv: Sequence[str] | None = (), *,
    service: TikTokPublicationService | None = None,
    stdout: TextIO = sys.stdout, stderr: TextIO = sys.stderr,
) -> int:
    args = build_parser().parse_args(argv)
    publisher = service or TikTokPublicationService.from_environment(
        ClipReviewQueue(), VideoManifest()
    )
    try:
        result = (
            publisher.store.list_attempts()
            if args.command == "list"
            else publisher.reconcile_pending(limit=args.limit)
        )
        print(json.dumps(result, indent=2, sort_keys=True), file=stdout)
    except (PublicationError, OSError, ValueError) as error:
        print(f"Publication operation failed safely: {error}", file=stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(None))
