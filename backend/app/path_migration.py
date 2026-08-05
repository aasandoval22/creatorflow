"""CLI for audited persistent-path normalization and ownership inventory."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

from backend.services.path_migration import (
    PathMigrationError,
    PathMigrationService,
    build_media_coverage,
)
from backend.services.persistent_paths import (
    DEFAULT_LEGACY_DATA_ROOTS,
    DEFAULT_PERSISTENT_DATA_ROOT,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan and recover audited persistent-media path normalization."
    )
    parser.add_argument("--data-root", type=Path, default=DEFAULT_PERSISTENT_DATA_ROOT)
    parser.add_argument(
        "--legacy-root", type=Path, action="append",
        help="Explicit recognized historical data root; may be repeated.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("inventory")
    commands.add_parser("plan")
    show = commands.add_parser("show")
    show.add_argument("--plan-id", required=True)
    apply = commands.add_parser("apply")
    apply.add_argument("--plan-id", required=True)
    apply.add_argument("--confirm", required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("--recovery-id", required=True)
    commands.add_parser("coverage")
    adopt = commands.add_parser("adopt-orphan")
    adopt.add_argument("--media-path", required=True)
    adopt.add_argument("--video-id", required=True)
    adopt.add_argument("--creator", required=True)
    adopt.add_argument("--checksum", required=True)
    adopt.add_argument("--evidence-path", action="append", required=True)
    adopt.add_argument("--confirm", required=True)
    return parser


def _summary(plan: dict[str, Any], stream: TextIO) -> None:
    print(f"Path migration plan: {plan['plan_id']}", file=stream)
    print(f"Checksum: {plan['content_sha256']}", file=stream)
    print(f"Proposed fields: {len(plan['changes'])}", file=stream)
    print(f"Manual review fields: {len(plan['manual_review'])}", file=stream)
    orphans = plan["orphan_analysis"]
    print(f"Proven orphan associations: {len(orphans['proven_associations'])}", file=stream)
    print(f"Unverified orphans: {len(orphans['orphaned_unverified'])}", file=stream)
    print(f"Conflicting ownership: {len(orphans['conflicting_ownership'])}", file=stream)
    for change in plan["changes"]:
        print(
            f"MIGRATE {change['schema']} {change['owning_record']} "
            f"{change['field_name']}: {change['old_value']} -> "
            f"{change['proposed_value']} ({change['size_bytes']} bytes, "
            f"sha256={change['checksum_sha256']})",
            file=stream,
        )
    for entry in plan["manual_review"]:
        print(
            f"MANUAL {entry['schema']} {entry.get('owning_record', 'document')} "
            f"{entry.get('field_name')}: {entry.get('value')} | "
            f"{entry['manual_reason']}", file=stream,
        )
    print("This plan does not rewrite records or move media.", file=stream)


def main(
    argv: Sequence[str] | None = (), *,
    service: PathMigrationService | None = None,
    stdout: TextIO = sys.stdout, stderr: TextIO = sys.stderr,
) -> int:
    args = build_parser().parse_args(argv)
    migration = service or PathMigrationService(
        args.data_root,
        legacy_roots=args.legacy_root or DEFAULT_LEGACY_DATA_ROOTS,
    )
    try:
        if args.command == "inventory":
            result = migration.inventory()
        elif args.command == "plan":
            result = migration.plan()
            _summary(result, stdout)
            return 0
        elif args.command == "show":
            result = migration.show(args.plan_id)
            _summary(result, stdout)
            return 0
        elif args.command == "apply":
            result = migration.apply(args.plan_id, confirm=args.confirm)
        elif args.command == "restore":
            result = migration.restore(args.recovery_id)
        elif args.command == "coverage":
            result = build_media_coverage(args.data_root)
        else:
            result = migration.adopt_orphan(
                media_path=args.media_path, video_id=args.video_id,
                creator=args.creator, checksum_sha256=args.checksum,
                evidence_paths=args.evidence_path, confirm=args.confirm,
            )
        print(json.dumps(result, indent=2, sort_keys=True), file=stdout)
    except (OSError, ValueError, PathMigrationError) as error:
        print(f"Path migration failed safely: {error}", file=stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(None))
