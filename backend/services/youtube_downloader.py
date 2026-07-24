from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yt_dlp

from backend.services.channel_manager import normalize_youtube_channel_url


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOWNLOAD_DIRECTORY = PROJECT_ROOT / "data" / "downloads"
ARCHIVE_DIRECTORY = PROJECT_ROOT / "data" / "database"
DOWNLOAD_ARCHIVE = ARCHIVE_DIRECTORY / "downloaded_videos.txt"


class DownloadStatus(str, Enum):
    SUCCESS = "success"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class DownloadResult:
    status: DownloadStatus
    url: str
    message: str
    downloaded_count: int = 0


class YouTubeDownloader:
    """Download individual videos or recent uploads from YouTube channels."""

    def __init__(
        self,
        download_directory: Path = DOWNLOAD_DIRECTORY,
        archive_directory: Path = ARCHIVE_DIRECTORY,
    ) -> None:
        self.download_directory = Path(download_directory)
        self.archive_directory = Path(archive_directory)
        self.download_archive = self.archive_directory / DOWNLOAD_ARCHIVE.name
        self.download_directory.mkdir(parents=True, exist_ok=True)
        self.archive_directory.mkdir(parents=True, exist_ok=True)

    def _build_options(self) -> dict[str, Any]:
        """Return the shared yt-dlp configuration."""

        return {
            "outtmpl": str(
                self.download_directory
                / "%(uploader)s"
                / "%(upload_date)s_%(id)s_%(title)s.%(ext)s"
            ),
            "format": "bestvideo*+bestaudio/best",
            "merge_output_format": "mp4",
            "restrictfilenames": True,
            "download_archive": str(self.download_archive),
            "ignoreerrors": False,
        }

    def download_video(self, video_url: str) -> DownloadResult:
        """Download one video URL."""

        options = self._build_options()
        options["noplaylist"] = True

        print(f"Downloading video to: {self.download_directory}")

        return self._download([video_url], video_url, options)

    def download_recent_channel_videos(
        self,
        channel_name: str,
        channel_url: str,
        max_videos: int = 3,
    ) -> DownloadResult:
        """Check a channel and download up to its newest videos."""

        if max_videos < 1:
            raise ValueError("max_videos must be at least 1.")

        normalized_channel_url = normalize_youtube_channel_url(channel_url)
        parsed = urlsplit(normalized_channel_url)
        videos_url = urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path.rstrip("/") + "/videos",
                "",
                "",
            )
        )

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

        return self._download([videos_url], videos_url, options)

    def _read_archive_entries(self) -> set[str]:
        if not self.download_archive.exists():
            return set()

        return {
            line.strip()
            for line in self.download_archive.read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        }

    def _download(
        self,
        urls: list[str],
        result_url: str,
        options: dict[str, Any],
    ) -> DownloadResult:
        before = self._read_archive_entries()

        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                exit_code = downloader.download(urls)
        except Exception as error:
            return DownloadResult(
                DownloadStatus.FAILED,
                result_url,
                f"yt-dlp failed: {error}",
            )

        if exit_code != 0:
            return DownloadResult(
                DownloadStatus.FAILED,
                result_url,
                f"yt-dlp exited with status {exit_code}.",
            )

        added_entries = self._read_archive_entries() - before

        if not added_entries:
            return DownloadResult(
                DownloadStatus.SKIPPED,
                result_url,
                "No new videos were downloaded.",
            )

        return DownloadResult(
            DownloadStatus.SUCCESS,
            result_url,
            f"Downloaded {len(added_entries)} new video(s).",
            downloaded_count=len(added_entries),
        )
