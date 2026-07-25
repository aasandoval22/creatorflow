"""Manage and compare entirely local accepted clip references."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from backend.services.clip_review_queue import DEFAULT_REVIEW_QUEUE_PATH, ClipReviewQueue
from backend.services.reference_clip_analyzer import ReferenceAnalysisError, ReferenceClipAnalyzer
from backend.services.reference_clip_comparator import ReferenceClipComparator
from backend.services.reference_clip_library import (
    DEFAULT_REFERENCE_INDEX, DEFAULT_REFERENCE_ROOT, ReferenceClipError, ReferenceClipLibrary,
)
from backend.services.reference_profile_builder import (
    DEFAULT_PROFILE_ROOT, ReferenceProfileBuilder, ReferenceProfileError,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage local accepted reference clips.")
    parser.add_argument("--index-path", type=Path, default=DEFAULT_REFERENCE_INDEX)
    parser.add_argument("--reference-root", type=Path, default=DEFAULT_REFERENCE_ROOT)
    parser.add_argument("--profile-directory", type=Path, default=DEFAULT_PROFILE_ROOT)
    sub = parser.add_subparsers(dest="command", required=True)
    register = sub.add_parser("register")
    register.add_argument("--reference-directory", type=Path)
    register.add_argument("--media-path", type=Path)
    register.add_argument("--baseline-path", type=Path)
    register.add_argument("--source-info-path", type=Path)
    register.add_argument("--reference-id")
    register.add_argument("--profile")
    register.add_argument("--index-path", type=Path, dest="command_index_path")
    analyze = sub.add_parser("analyze")
    analyze.add_argument("reference_id")
    analyze.add_argument("--no-transcription", action="store_true")
    analyze.add_argument("--force", action="store_true")
    analyze.add_argument("--ffmpeg-path", default="ffmpeg")
    analyze.add_argument("--ffprobe-path", default="ffprobe")
    analyze.add_argument("--model", default="base.en")
    analyze.add_argument("--device", default="cpu")
    analyze.add_argument("--compute-type", default="int8")
    build = sub.add_parser("build-profile")
    build.add_argument("profile_name")
    compare = sub.add_parser("compare")
    compare.add_argument("--profile", required=True)
    compare.add_argument("--video-id")
    compare.add_argument("--status", choices=("pending", "rejected"), default="rejected")
    compare.add_argument("--review-queue-path", type=Path, default=DEFAULT_REVIEW_QUEUE_PATH)
    listing = sub.add_parser("list")
    listing.add_argument("--status")
    listing.add_argument("--creator")
    listing.add_argument("--profile")
    show = sub.add_parser("show"); show.add_argument("reference_id")
    validate = sub.add_parser("validate"); validate.add_argument("reference_id")
    profile = sub.add_parser("show-profile"); profile.add_argument("profile_name")
    return parser


def main(argv: Sequence[str] | None = ()) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    index = getattr(args, "command_index_path", None) or args.index_path
    library = ReferenceClipLibrary(args.reference_root, index)
    builder = ReferenceProfileBuilder(library, args.profile_directory)
    try:
        if args.command == "register":
            if args.reference_directory:
                entry = library.register_directory(
                    args.reference_directory, media_path=args.media_path,
                    baseline_path=args.baseline_path, source_info_path=args.source_info_path,
                    reference_id=args.reference_id, profile_name=args.profile,
                )
            else:
                if args.media_path is None or args.baseline_path is None:
                    parser.error("register requires --reference-directory or both --media-path and --baseline-path")
                entry = library.register(
                    media_path=args.media_path, baseline_path=args.baseline_path,
                    source_info_path=args.source_info_path, reference_id=args.reference_id,
                    profile_name=args.profile,
                )
            print(f"Registered {entry['reference_id']}")
            print(f"Index: {index}")
            print(f"SHA-256: {entry['checksum_sha256']}")
        elif args.command == "analyze":
            analyzer = ReferenceClipAnalyzer(
                library, ffmpeg_path=args.ffmpeg_path, ffprobe_path=args.ffprobe_path,
                model_name=args.model, device=args.device, compute_type=args.compute_type,
            )
            analysis = analyzer.analyze(
                args.reference_id, transcription=not args.no_transcription, force=args.force
            )
            print(f"Analyzed {args.reference_id}: {library.get(args.reference_id)['analysis_path']}")
            print(json.dumps({"media": analysis["media"], "speech": analysis["speech"]}, indent=2))
        elif args.command == "build-profile":
            profile = builder.build(args.profile_name)
            print(f"Built profile: {builder.profile_path(args.profile_name)}")
            print(f"Confidence: {profile['confidence']}")
            if profile["confidence"] == "provisional":
                print("Provisional: one reference does not establish statistical confidence.")
        elif args.command == "compare":
            queue = ClipReviewQueue(args.review_queue_path)
            comparator = ReferenceClipComparator(builder)
            reports = comparator.compare_reviews(
                args.profile, queue.list_items(), status=args.status,
                video_id=args.video_id, write=True,
            )
            print(f"Compared {len(reports)} preview(s) against {args.profile}.")
            for report in reports:
                print(f"{report['review_id']}: {comparator.report_path(args.profile, report['review_id'])}")
                for name, finding in report["findings"].items():
                    print(f"  {name}: {finding['status']} — {finding['evidence']}")
        elif args.command == "list":
            entries = library.list_references(
                status=args.status, creator=args.creator, profile_name=args.profile
            )
            for entry in entries:
                print(f"{entry['reference_id']}\t{entry['status']}\t{entry['profile_name']}\t{entry['creator']}")
            print(f"{len(entries)} reference(s).")
        elif args.command == "show":
            print(json.dumps(library.get(args.reference_id), indent=2))
        elif args.command == "validate":
            library.validate_checksum(args.reference_id)
            print(f"{args.reference_id}: checksum valid")
        elif args.command == "show-profile":
            print(json.dumps(builder.read(args.profile_name), indent=2))
        return 0
    except (ReferenceClipError, ReferenceAnalysisError, ReferenceProfileError,
            OSError, ValueError) as error:
        print(f"reference-clips: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(None))
