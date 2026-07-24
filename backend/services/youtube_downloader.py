from pathlib import Path
from typing import Any

import yt_dlp


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOWNLOAD_DIRECTORY = PROJECT_ROOT / "data" / "downloads"
ARCHIVE_DIRECTORY = PROJECT_ROOT / "data" / "database"
DOWNLOAD_ARCHIVE = ARCHIVE_DIRECTORY / "downloaded_videos.txt"


class YouTubeDownloader:
    """Download individual videos or recent uploads from YouTube channels."""

    def __init__(self) -> None:
        DOWNLOAD_DIRECTORY.mkdir(parents=True, exist_ok=True)
        ARCHIVE_DIRECTORY.mkdir(parents=True, exist_ok=True)

    def _build_options(self) -> dict[str, Any]:
        """Return the shared yt-dlp configuration."""

        return {
            "outtmpl": str(
                DOWNLOAD_DIRECTORY
                / "%(uploader)s"
                / "%(upload_date)s_%(id)s_%(title)s.%(ext)s"
            ),
            "format": "bestvideo*+bestaudio/best",
            "merge_output_format": "mp4",
            "restrictfilenames": True,
            "download_archive": str(DOWNLOAD_ARCHIVE),
            "ignoreerrors": True,
        }

    def download_video(self, video_url: str) -> None:
        """Download one video URL."""

        options = self._build_options()
        options["noplaylist"] = True

        print(f"Downloading video to: {DOWNLOAD_DIRECTORY}")

        with yt_dlp.YoutubeDL(options) as downloader:
            downloader.download([video_url])

    def download_recent_channel_videos(
        self,
        channel_name: str,
        channel_url: str,
        max_videos: int = 3,
    ) -> None:
        """Check a channel and download up to its newest videos."""

        if max_videos < 1:
            raise ValueError("max_videos must be at least 1.")

        videos_url = channel_url.rstrip("/") + "/videos"

        options = self._build_options()
        options.update(
            {
                "playlistend": max_videos,
                "lazy_playlist": True,
            }
        )

        print(
            f"Checking {channel_name} for its "
            f"{max_videos} newest video(s)..."
        )

        with yt_dlp.YoutubeDL(options) as downloader:
            downloader.download([videos_url])
