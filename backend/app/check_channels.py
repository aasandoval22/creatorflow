import yt_dlp

from backend.services.channel_manager import ChannelManager
from backend.services.youtube_downloader import YouTubeDownloader


def main() -> None:
    """Check every enabled channel for recent uploads."""

    manager = ChannelManager()
    downloader = YouTubeDownloader()

    try:
        channels = manager.get_enabled_channels()
    except (FileNotFoundError, ValueError) as error:
        print(f"Configuration error: {error}")
        raise SystemExit(1) from error

    if not channels:
        print("No enabled channels were found.")
        return

    print(f"Checking {len(channels)} enabled channel(s).")

    for channel in channels:
        try:
            downloader.download_recent_channel_videos(
                channel_name=channel["name"],
                channel_url=channel["youtube_url"],
                max_videos=3,
            )
        except yt_dlp.utils.DownloadError as error:
            print(f"Failed to check {channel['name']}: {error}")


if __name__ == "__main__":
    main()
