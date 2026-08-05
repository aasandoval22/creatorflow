"""Conservative CLI for ownership-aware media retention."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

from backend.services.media_lifecycle import (
    DEFAULT_DATA_ROOT,
    DEFAULT_POLICY_PATH,
    MediaCleanupService,
    MediaLifecycleError,
    MediaOwnershipGraph,
    RetentionPolicy,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan and perform two-stage CreatorFlow media retention."
    )
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--policy-path", type=Path, default=DEFAULT_POLICY_PATH)
    parser.add_argument("--cleanup-root", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("ownership")
    commands.add_parser("plan")
    show = commands.add_parser("show")
    show.add_argument("--plan-id", required=True)
    apply = commands.add_parser("apply")
    apply.add_argument("--plan-id", required=True)
    apply.add_argument("--confirm", required=True)
    apply.add_argument(
        "--execute", action="store_true",
        help="Move revalidated eligible files to quarantine; default is dry-run.",
    )
    restore = commands.add_parser("restore")
    restore.add_argument("--quarantine-id", required=True)
    purge = commands.add_parser("purge")
    purge.add_argument("--quarantine-id", required=True)
    purge.add_argument("--confirm", required=True)
    commands.add_parser("status")
    automatic = commands.add_parser("run-eligible")
    automatic.add_argument(
        "--execute", action="store_true",
        help="Quarantine eligible media; never purges. Default is dry-run.",
    )
    return parser


def _service(args: argparse.Namespace) -> MediaCleanupService:
    graph = MediaOwnershipGraph(data_root=args.data_root)
    return MediaCleanupService(
        graph,
        policy=RetentionPolicy.read(args.policy_path),
        cleanup_root=args.cleanup_root,
    )


def _print_plan(plan: dict[str, Any], stream: TextIO) -> None:
    print(
        f"Cleanup plan {plan['plan_id']} (checksum {plan['content_sha256']})",
        file=stream,
    )
    for item in plan["items"]:
        status = "ELIGIBLE" if item["eligible"] else "RETAIN"
        print(
            f"{status} {item['relative_path']} | {item['size_bytes']} bytes | "
            f"eligible_at={item['eligible_at'] or 'not established'} | "
            f"{' ; '.join(item['reasons'])}",
            file=stream,
        )
    print(
        "Planning does not move or delete media. Apply is also a dry run unless "
        "--execute is supplied.",
        file=stream,
    )


def main(
    argv: Sequence[str] | None = (), *,
    service: MediaCleanupService | None = None,
    stdout: TextIO = sys.stdout, stderr: TextIO = sys.stderr,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        cleanup = service or _service(args)
        if args.command == "ownership":
            result = cleanup.ownership()
        elif args.command == "plan":
            result = cleanup.plan()
            _print_plan(result, stdout)
            return 0
        elif args.command == "show":
            result = cleanup.show(args.plan_id)
            _print_plan(result, stdout)
            return 0
        elif args.command == "apply":
            result = cleanup.apply(
                args.plan_id, confirm=args.confirm, execute=args.execute
            )
        elif args.command == "restore":
            result = cleanup.restore(args.quarantine_id)
        elif args.command == "purge":
            result = cleanup.purge(
                args.quarantine_id, confirm=args.confirm
            )
        elif args.command == "status":
            result = cleanup.status()
        else:
            result = cleanup.run_eligible(execute=args.execute)
        print(json.dumps(result, indent=2, sort_keys=True), file=stdout)
    except (MediaLifecycleError, OSError, ValueError) as error:
        print(f"Media cleanup failed safely: {error}", file=stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(None))
