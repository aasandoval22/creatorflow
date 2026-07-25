import argparse
from collections.abc import Sequence
from pathlib import Path

from backend.services.video_manifest import DEFAULT_MANIFEST_PATH, ManifestError
from backend.services.video_transcriber import (
    DEFAULT_TRANSCRIPT_DIRECTORY,
    VideoTranscriber,
)


def positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not an integer"
        ) from error
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Transcribe downloaded CreatorFlow videos locally."
    )
    parser.add_argument(
        "--manifest-path", type=Path, default=DEFAULT_MANIFEST_PATH
    )
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=DEFAULT_TRANSCRIPT_DIRECTORY,
    )
    parser.add_argument("--model", default="base.en")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--compute-type", default="int8")
    parser.add_argument("--language", default="en")
    parser.add_argument("--limit", type=positive_integer)
    parser.add_argument("--video-id")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv: Sequence[str] | None = ()) -> int:
    args = build_parser().parse_args(argv)
    try:
        transcriber = VideoTranscriber(
            manifest_path=args.manifest_path,
            output_directory=args.output_directory,
            model_name=args.model,
            device=args.device,
            compute_type=args.compute_type,
            language=args.language,
        )
        if (
            args.video_id is not None
            and transcriber.manifest.get(args.video_id) is None
        ):
            print(f"Video ID {args.video_id!r} was not found in the manifest.")
            return 1
        batch = transcriber.transcribe(
            limit=args.limit,
            video_id=args.video_id,
            retry_failed=args.retry_failed,
            force=args.force,
        )
    except (ManifestError, OSError, ValueError) as error:
        print(f"Transcription configuration error: {error}")
        return 1

    for result in batch.results:
        print(
            f"{result.video_id}: {result.status.value} - {result.message}"
        )
    print(
        f"Summary: {batch.successful} successful, "
        f"{batch.skipped} skipped, {batch.failed} failed."
    )
    return 1 if batch.failed else 0


if __name__ == "__main__":
    raise SystemExit(main(None))
