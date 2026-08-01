"""Manage and compare entirely local accepted clip references."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
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
from backend.services.reference_annotations import (
    DEFAULT_ANNOTATION_ROOT, ENUM_FIELDS, ReferenceAnnotationError,
    ReferenceAnnotationStore,
)
from backend.services.reference_evidence_audit import (
    ReferenceEvidenceAuditError, ReferenceEvidenceAuditLedger,
)
from backend.services.reference_evidence_service import (
    ReferenceEvidenceError, ReferenceEvidenceService,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage local accepted reference clips.")
    parser.add_argument("--index-path", type=Path, default=DEFAULT_REFERENCE_INDEX)
    parser.add_argument("--reference-root", type=Path, default=DEFAULT_REFERENCE_ROOT)
    parser.add_argument("--profile-directory", type=Path, default=DEFAULT_PROFILE_ROOT)
    parser.add_argument(
        "--annotation-directory", type=Path, default=DEFAULT_ANNOTATION_ROOT
    )
    parser.add_argument("--evidence-audit-path", type=Path)
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
    analyze.add_argument("reference_id", nargs="?")
    analyze.add_argument("--reference-id", dest="reference_id_option")
    transcription = analyze.add_mutually_exclusive_group()
    transcription.add_argument("--with-transcription", action="store_true")
    transcription.add_argument("--no-transcription", action="store_true")
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
    annotations = sub.add_parser("show-annotations")
    annotations.add_argument("reference_id")
    annotate = sub.add_parser("annotate")
    annotate.add_argument("reference_id")
    annotate.add_argument("--expected-revision", type=int, required=True)
    for name, choices in ENUM_FIELDS.items():
        annotate.add_argument(
            f"--{name.replace('_', '-')}", choices=sorted(choices)
        )
    annotate.add_argument("--desired-quality", action="append")
    annotate.add_argument("--undesirable-quality", action="append")
    annotate.add_argument("--reviewer-notes")
    history = sub.add_parser("evidence-history")
    history.add_argument("reference_id")
    history.add_argument("--limit", type=int)
    return parser


def main(argv: Sequence[str] | None = ()) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "analyze":
        positional = args.reference_id
        explicit = args.reference_id_option
        if positional and explicit and positional != explicit:
            parser.error("analyze reference IDs disagree")
        args.reference_id = explicit or positional
        if args.reference_id is None:
            parser.error("analyze requires a reference ID or --reference-id")
    index = getattr(args, "command_index_path", None) or args.index_path
    library = ReferenceClipLibrary(args.reference_root, index)
    annotation_store = ReferenceAnnotationStore(args.annotation_directory)
    builder = ReferenceProfileBuilder(
        library, args.profile_directory, annotation_store=annotation_store
    )
    audit = ReferenceEvidenceAuditLedger(
        args.evidence_audit_path
        if args.evidence_audit_path is not None
        else args.annotation_directory / "events.jsonl"
    )
    analyzer_factory = lambda reference_library: ReferenceClipAnalyzer(
        reference_library,
        ffmpeg_path=getattr(args, "ffmpeg_path", "ffmpeg"),
        ffprobe_path=getattr(args, "ffprobe_path", "ffprobe"),
        model_name=getattr(args, "model", "base.en"),
        device=getattr(args, "device", "cpu"),
        compute_type=getattr(args, "compute_type", "int8"),
    )
    evidence = ReferenceEvidenceService(
        library,
        annotations=annotation_store,
        audit=audit,
        profile_builder=builder,
        analyzer_factory=analyzer_factory,
        lock_path=args.annotation_directory / ".evidence.lock",
    )
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
            analysis = evidence.reanalyze(
                args.reference_id,
                transcription=not args.no_transcription,
                force=args.force,
                request_id=uuid.uuid4().hex,
            )
            print(f"Analyzed {args.reference_id}: {library.get(args.reference_id)['analysis_path']}")
            print(json.dumps({
                "analysis_revision": analysis.get("analysis_revision", 0),
                "transcription": analysis.get("transcription", {}),
                "media": analysis["media"],
                "speech": analysis["speech"],
            }, indent=2))
        elif args.command == "build-profile":
            profile = evidence.rebuild_profile(
                args.profile_name, request_id=uuid.uuid4().hex
            )
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
        elif args.command == "show-annotations":
            evidence.accepted_entry(args.reference_id)
            print(json.dumps(annotation_store.read(args.reference_id), indent=2))
        elif args.command == "annotate":
            current = annotation_store.read(args.reference_id)["annotations"]
            values = dict(current)
            for name in ENUM_FIELDS:
                supplied = getattr(args, name)
                if supplied is not None:
                    values[name] = supplied
            if args.desired_quality is not None:
                values["desired_qualities"] = args.desired_quality
            if args.undesirable_quality is not None:
                values["undesirable_qualities"] = args.undesirable_quality
            if args.reviewer_notes is not None:
                values["reviewer_notes"] = args.reviewer_notes
            updated = evidence.update_annotations(
                args.reference_id,
                expected_revision=args.expected_revision,
                values=values,
                request_id=uuid.uuid4().hex,
            )
            print(
                f"Updated {args.reference_id} annotations to revision "
                f"{updated['revision']}."
            )
        elif args.command == "evidence-history":
            events = evidence.history(
                reference_id=args.reference_id, limit=args.limit
            )
            print(json.dumps(events, indent=2, sort_keys=True))
            print(f"{len(events)} evidence event(s).")
        return 0
    except (
        ReferenceAnnotationError,
        ReferenceClipError,
        ReferenceAnalysisError,
        ReferenceEvidenceAuditError,
        ReferenceEvidenceError,
        ReferenceProfileError,
        OSError,
        ValueError,
    ) as error:
        print(f"reference-clips: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(None))
