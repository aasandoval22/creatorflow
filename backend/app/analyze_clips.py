import argparse
from collections.abc import Sequence
from pathlib import Path

from backend.services.clip_candidate_generator import (
    DEFAULT_CANDIDATE_DIRECTORY,
    CandidateConfiguration,
    ClipCandidateGenerator,
)
from backend.services.video_manifest import DEFAULT_MANIFEST_PATH, ManifestError


def positive_number(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive number")
    return parsed


def positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer") from error
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rank local transcript moments as short-form clip candidates."
    )
    parser.add_argument("--manifest-path", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument(
        "--output-directory", type=Path, default=DEFAULT_CANDIDATE_DIRECTORY
    )
    parser.add_argument("--video-id")
    parser.add_argument("--limit", type=positive_integer)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--minimum-duration", type=positive_number, default=20)
    parser.add_argument("--target-duration", type=positive_number, default=35)
    parser.add_argument("--maximum-duration", type=positive_number, default=60)
    parser.add_argument("--maximum-candidates", type=positive_integer, default=10)
    parser.add_argument("--padding-before", type=float, default=0.15)
    parser.add_argument("--padding-after", type=float, default=0.25)
    parser.add_argument("--minimum-boundary-confidence", type=float, default=0.55)
    parser.add_argument(
        "--show-score-breakdown",
        action="store_true",
        help="Print component scores and candidate-specific reasons.",
    )
    return parser


def main(argv: Sequence[str] | None = ()) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        configuration = CandidateConfiguration(
            minimum_duration_seconds=args.minimum_duration,
            target_duration_seconds=args.target_duration,
            maximum_duration_seconds=args.maximum_duration,
            maximum_candidates=args.maximum_candidates,
            padding_before_seconds=args.padding_before,
            padding_after_seconds=args.padding_after,
            minimum_boundary_confidence=args.minimum_boundary_confidence,
        )
        generator = ClipCandidateGenerator(
            manifest_path=args.manifest_path,
            output_directory=args.output_directory,
            configuration=configuration,
        )
        if args.video_id and generator.manifest.get(args.video_id) is None:
            print(f"Video ID {args.video_id!r} was not found in the manifest.")
            return 1
        batch = generator.analyze(
            video_id=args.video_id, limit=args.limit, force=args.force
        )
    except (ManifestError, OSError, ValueError) as error:
        print(f"Clip-analysis configuration error: {error}")
        return 1

    for result in batch.results:
        print(f"{result.video_id}: {result.status.value} - {result.message}")
        for candidate in result.candidates:
            print(
                f"  #{candidate['rank']} {candidate['start']:.3f}-"
                f"{candidate['end']:.3f} ({candidate['duration']:.3f}s) "
                f"score {candidate['score']:.1f}"
            )
            if args.show_score_breakdown:
                print(
                    f"    Ending classification: "
                    f"{candidate['ending_classification']}"
                )
                boundary = candidate["boundary_details"]
                print(
                    "    Boundaries: "
                    f"start={boundary['start_method']} "
                    f"({boundary['start_confidence']:.2f}), "
                    f"end={boundary['end_method']} "
                    f"({boundary['end_confidence']:.2f})"
                )
                components = ", ".join(
                    f"{name}={value:.1f}"
                    for name, value in candidate["component_scores"].items()
                )
                positives = [
                    reason for reason in candidate["reasons"]
                    if not reason.startswith("Penalized")
                ]
                penalties = [
                    reason for reason in candidate["reasons"]
                    if reason.startswith("Penalized")
                ]
                print(f"    Components: {components}")
                print("    Component rounding tolerance: 0.1")
                print(f"    Positive reasons: {'; '.join(positives) or 'None'}")
                print(f"    Penalties: {'; '.join(penalties) or 'None'}")
    print(
        f"Summary: {batch.successful} successful, "
        f"{batch.skipped} skipped, {batch.failed} failed."
    )
    return 1 if batch.failed else 0


if __name__ == "__main__":
    raise SystemExit(main(None))
