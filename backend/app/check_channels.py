import argparse
from collections.abc import Sequence
from pathlib import Path

from backend.services.channel_manager import ChannelManager, channel_video_limit
from backend.services.video_manifest import DEFAULT_MANIFEST_PATH, ManifestError
from backend.services.youtube_downloader import DownloadStatus, YouTubeDownloader


def positive_integer(value: str) -> int:
    """Parse a command-line value as a positive integer."""

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
        description="Discover and download recent CreatorFlow channel videos."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="record video metadata without downloading media",
    )
    parser.add_argument(
        "--max-videos",
        type=positive_integer,
        default=3,
        help="maximum recent videos per channel (default: 3)",
    )
    parser.add_argument(
        "--manifest-path",
        type=Path,
        default=DEFAULT_MANIFEST_PATH,
        help=f"video manifest path (default: {DEFAULT_MANIFEST_PATH})",
    )
    return parser


def main(argv: Sequence[str] | None = ()) -> int:
    """Check every enabled channel for recent uploads."""

    args = build_parser().parse_args(argv)
    manager = ChannelManager()

    try:
        downloader = YouTubeDownloader(manifest_path=args.manifest_path)
        channels = manager.get_enabled_channels()
    except (FileNotFoundError, ValueError, ManifestError) as error:
        print(f"Configuration error: {error}")
        return 1

    if args.dry_run:
        print("Dry-run mode is active; no media will be downloaded.")

    if not channels:
        print("No enabled channels were found.")
        print("Summary: 0 successful, 0 skipped, 0 failed.")
        return 0

    print(f"Checking {len(channels)} enabled channel(s).")
    counts = {status: 0 for status in DownloadStatus}

    for channel in channels:
        try:
            maximum = channel_video_limit(channel, args.max_videos)
            if args.dry_run:
                result = downloader.discover_recent_channel_videos(
                    channel_name=channel["name"],
                    channel_url=channel["youtube_url"],
                    max_videos=maximum,
                )
            else:
                result = downloader.download_recent_channel_videos(
                    channel_name=channel["name"],
                    channel_url=channel["youtube_url"],
                    max_videos=maximum,
                )
        except (TypeError, ValueError, ManifestError) as error:
            counts[DownloadStatus.FAILED] += 1
            print(f"Failed to check {channel['name']}: {error}")
            continue

        counts[result.status] += 1
        print(f"{channel['name']}: {result.status.value} - {result.message}")

    print(
        "Summary: "
        f"{counts[DownloadStatus.SUCCESS]} successful, "
        f"{counts[DownloadStatus.SKIPPED]} skipped, "
        f"{counts[DownloadStatus.FAILED]} failed."
    )
    return 1 if counts[DownloadStatus.FAILED] else 0


if __name__ == "__main__":
    raise SystemExit(main(None))
