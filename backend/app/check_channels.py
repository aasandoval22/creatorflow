from backend.services.channel_manager import ChannelManager
from backend.services.youtube_downloader import DownloadStatus, YouTubeDownloader


def main() -> int:
    """Check every enabled channel for recent uploads."""

    manager = ChannelManager()
    downloader = YouTubeDownloader()

    try:
        channels = manager.get_enabled_channels()
    except (FileNotFoundError, ValueError) as error:
        print(f"Configuration error: {error}")
        return 1

    if not channels:
        print("No enabled channels were found.")
        print("Summary: 0 successful, 0 skipped, 0 failed.")
        return 0

    print(f"Checking {len(channels)} enabled channel(s).")
    counts = {status: 0 for status in DownloadStatus}

    for channel in channels:
        try:
            result = downloader.download_recent_channel_videos(
                channel_name=channel["name"],
                channel_url=channel["youtube_url"],
                max_videos=3,
            )
        except (TypeError, ValueError) as error:
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
    raise SystemExit(main())
