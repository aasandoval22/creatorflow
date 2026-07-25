import os
import shutil
import warnings
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

import yt_dlp

from backend.services.channel_manager import normalize_youtube_channel_url
from backend.services.video_manifest import (
    DEFAULT_MANIFEST_PATH,
    VideoManifest,
    VideoStatus,
    default_transcription,
    utc_now,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOWNLOAD_DIRECTORY = PROJECT_ROOT / "data" / "downloads"
ARCHIVE_DIRECTORY = PROJECT_ROOT / "data" / "database"
DOWNLOAD_ARCHIVE = ARCHIVE_DIRECTORY / "downloaded_videos.txt"
DEFAULT_DENO_PATH = Path.home() / ".deno" / "bin" / "deno"


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


@dataclass(frozen=True)
class ChannelDiscoveryResult:
    """Read-only recent-video discovery result."""

    status: DownloadStatus
    channel_name: str
    channel_url: str
    entries: tuple[dict[str, Any], ...]
    message: str


def find_deno_executable(explicit_path: Path | str | None = None) -> Path | None:
    """Return an executable Deno path without relying solely on shell PATH."""

    configured = os.environ.get("AUTOCLIP_DENO_PATH")
    if explicit_path is not None:
        candidates = [Path(explicit_path).expanduser()]
    else:
        candidates = []
        if configured:
            candidates.append(Path(configured).expanduser())
        candidates.append(DEFAULT_DENO_PATH)
        if found := shutil.which("deno"):
            candidates.append(Path(found))
    for candidate in candidates:
        absolute = Path(os.path.abspath(candidate))
        if absolute.is_file() and os.access(absolute, os.X_OK):
            return absolute
    return None


class YouTubeDownloader:
    """Discover and download YouTube videos with structured tracking."""

    def __init__(
        self,
        download_directory: Path = DOWNLOAD_DIRECTORY,
        archive_directory: Path = ARCHIVE_DIRECTORY,
        manifest_path: Path = DEFAULT_MANIFEST_PATH,
        manifest: VideoManifest | None = None,
        *,
        discovery_only: bool = False,
        deno_path: Path | str | None = None,
    ) -> None:
        self.download_directory = Path(download_directory)
        self.archive_directory = Path(archive_directory)
        self.download_archive = self.archive_directory / DOWNLOAD_ARCHIVE.name
        self.discovery_only = discovery_only
        self.deno_path = find_deno_executable(deno_path)
        if self.deno_path is None:
            warnings.warn(
                "Deno JavaScript runtime was not found. yt-dlp will continue "
                "without an explicitly configured JavaScript runtime; some "
                "YouTube formats may be unavailable. Set AUTOCLIP_DENO_PATH "
                "or install Deno at ~/.deno/bin/deno.",
                RuntimeWarning,
                stacklevel=2,
            )
        if not discovery_only:
            self.download_directory.mkdir(parents=True, exist_ok=True)
            self.archive_directory.mkdir(parents=True, exist_ok=True)
        self.manifest = manifest or (
            None if discovery_only else VideoManifest(manifest_path)
        )

    def _build_options(self) -> dict[str, Any]:
        """Return the shared yt-dlp download configuration."""

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
            **self._runtime_options(),
        }

    def _metadata_options(self, max_videos: int | None = None) -> dict[str, Any]:
        options: dict[str, Any] = {
            "skip_download": True,
            "quiet": True,
            "ignoreerrors": False,
            **self._runtime_options(),
        }
        if max_videos is not None:
            options["playlistend"] = max_videos
            options["lazy_playlist"] = True
        return options

    def _runtime_options(self) -> dict[str, Any]:
        if self.deno_path is None:
            return {}
        return {
            "js_runtimes": {
                "deno": {"path": str(self.deno_path)},
            },
        }

    def download_video(self, video_url: str) -> DownloadResult:
        """Discover metadata and download one video URL."""

        print(f"Downloading video to: {self.download_directory}")
        metadata_result = self._extract_metadata(video_url)
        if isinstance(metadata_result, DownloadResult):
            video_id = self._video_id_from_url(video_url)
            if video_id is not None:
                failed_record = self._record_from_metadata(
                    {"id": video_id, "webpage_url": video_url}, None, None
                )
                self._update_status(
                    failed_record,
                    VideoStatus.FAILED,
                    error_message=metadata_result.message,
                )
            return metadata_result
        return self._process_entries(
            [metadata_result], video_url, None, None, download=True
        )

    def discover_recent_channel_videos(
        self,
        channel_name: str,
        channel_url: str,
        max_videos: int = 3,
    ) -> DownloadResult:
        """Record recent channel video metadata without downloading media."""

        videos_url, normalized_url = self._channel_urls(
            channel_url, max_videos
        )
        print(
            f"Discovering {channel_name}'s {max_videos} newest video(s)..."
        )
        metadata_result = self._extract_metadata(videos_url, max_videos)
        if isinstance(metadata_result, DownloadResult):
            return metadata_result
        entries = self._entries_from_metadata(metadata_result)
        return self._process_entries(
            entries,
            videos_url,
            channel_name,
            normalized_url,
            download=False,
        )

    def discover_recent_channel_metadata(
        self,
        channel_name: str,
        channel_url: str,
        max_videos: int = 3,
    ) -> ChannelDiscoveryResult:
        """Return recent metadata without changing manifests or downloading."""

        videos_url, normalized_url = self._channel_urls(channel_url, max_videos)
        metadata_result = self._extract_metadata(videos_url, max_videos)
        if isinstance(metadata_result, DownloadResult):
            return ChannelDiscoveryResult(
                DownloadStatus.FAILED, channel_name, normalized_url, (),
                metadata_result.message,
            )
        entries = tuple(self._entries_from_metadata(metadata_result))
        if not entries:
            return ChannelDiscoveryResult(
                DownloadStatus.FAILED, channel_name, normalized_url, (),
                "Metadata collection failed: no video entries were returned.",
            )
        return ChannelDiscoveryResult(
            DownloadStatus.SUCCESS, channel_name, normalized_url, entries,
            f"Discovered {len(entries)} recent video(s).",
        )

    def download_discovered_entry(
        self,
        metadata: dict[str, Any],
        channel_name: str,
        channel_url: str,
    ) -> DownloadResult:
        """Download one previously discovered metadata entry."""

        video_id = metadata.get("id")
        result_url = (
            metadata.get("webpage_url")
            if isinstance(metadata.get("webpage_url"), str)
            else f"https://www.youtube.com/watch?v={video_id}"
        )
        normalized_url = normalize_youtube_channel_url(channel_url)
        return self._process_entries(
            [metadata], result_url, channel_name, normalized_url, download=True
        )

    def download_recent_channel_videos(
        self,
        channel_name: str,
        channel_url: str,
        max_videos: int = 3,
    ) -> DownloadResult:
        """Discover and download up to a channel's newest videos."""

        videos_url, normalized_url = self._channel_urls(
            channel_url, max_videos
        )
        print(
            f"Checking {channel_name} for its "
            f"{max_videos} newest video(s)..."
        )
        metadata_result = self._extract_metadata(videos_url, max_videos)
        if isinstance(metadata_result, DownloadResult):
            return metadata_result
        entries = self._entries_from_metadata(metadata_result)
        return self._process_entries(
            entries,
            videos_url,
            channel_name,
            normalized_url,
            download=True,
        )

    def _channel_urls(
        self, channel_url: str, max_videos: int
    ) -> tuple[str, str]:
        if max_videos < 1:
            raise ValueError("max_videos must be at least 1.")

        normalized_url = normalize_youtube_channel_url(channel_url)
        parsed = urlsplit(normalized_url)
        videos_url = urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path.rstrip("/") + "/videos",
                "",
                "",
            )
        )
        return videos_url, normalized_url

    def _extract_metadata(
        self, url: str, max_videos: int | None = None
    ) -> dict[str, Any] | DownloadResult:
        try:
            with yt_dlp.YoutubeDL(
                self._metadata_options(max_videos)
            ) as downloader:
                metadata = downloader.extract_info(url, download=False)
        except Exception as error:
            return DownloadResult(
                DownloadStatus.FAILED,
                url,
                f"Metadata collection failed: {error}",
            )

        if not isinstance(metadata, dict):
            return DownloadResult(
                DownloadStatus.FAILED,
                url,
                "Metadata collection failed: yt-dlp returned no metadata.",
            )
        return metadata

    @staticmethod
    def _entries_from_metadata(
        metadata: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if "entries" not in metadata:
            return [metadata]
        entries = metadata.get("entries")
        if not isinstance(entries, Iterable) or isinstance(
            entries, (str, bytes, dict)
        ):
            return []
        return [entry for entry in entries if isinstance(entry, dict)]

    @staticmethod
    def _video_id_from_url(video_url: str) -> str | None:
        parsed = urlsplit(video_url)
        values = parse_qs(parsed.query).get("v", [])
        if values and values[0].strip():
            return values[0].strip()
        if parsed.hostname in {"youtu.be", "www.youtu.be"}:
            candidate = parsed.path.strip("/")
            return candidate or None
        return None

    def _process_entries(
        self,
        entries: list[dict[str, Any]],
        result_url: str,
        channel_name: str | None,
        channel_url: str | None,
        download: bool,
    ) -> DownloadResult:
        if self.manifest is None:
            raise RuntimeError(
                "This downloader was initialized for read-only discovery."
            )
        if not entries:
            return DownloadResult(
                DownloadStatus.FAILED,
                result_url,
                "Metadata collection failed: no video entries were returned.",
            )

        downloaded_count = 0
        skipped_count = 0
        discovered_count = 0
        failures: list[str] = []

        for metadata in entries:
            video_id = metadata.get("id")
            if not isinstance(video_id, str) or not video_id.strip():
                failures.append("metadata did not include a video ID")
                continue

            record = self._record_from_metadata(
                metadata, channel_name, channel_url
            )
            self.manifest.upsert(record)
            discovered_count += 1

            if not download:
                continue

            outcome = self._download_discovered_video(metadata, record)
            if outcome is VideoStatus.DOWNLOADED:
                downloaded_count += 1
            elif outcome is VideoStatus.SKIPPED:
                skipped_count += 1
            else:
                failed_record = self.manifest.get(video_id)
                failures.append(
                    (failed_record or {}).get("error_message")
                    or f"{video_id}: download failed"
                )

        if failures:
            return DownloadResult(
                DownloadStatus.FAILED,
                result_url,
                f"{len(failures)} video(s) failed: {'; '.join(failures)}",
                downloaded_count=downloaded_count,
            )

        if not download:
            return DownloadResult(
                DownloadStatus.SUCCESS,
                result_url,
                f"Discovered {discovered_count} video(s); no media downloaded.",
            )

        if downloaded_count:
            return DownloadResult(
                DownloadStatus.SUCCESS,
                result_url,
                f"Downloaded {downloaded_count} new video(s)"
                + (
                    f"; skipped {skipped_count}."
                    if skipped_count
                    else "."
                ),
                downloaded_count=downloaded_count,
            )

        return DownloadResult(
            DownloadStatus.SKIPPED,
            result_url,
            f"No new videos were downloaded; skipped {skipped_count}.",
        )

    def _record_from_metadata(
        self,
        metadata: dict[str, Any],
        channel_name: str | None,
        channel_url: str | None,
    ) -> dict[str, Any]:
        video_id = str(metadata["id"]).strip()
        video_url = metadata.get("webpage_url")
        if not isinstance(video_url, str) or not video_url.strip():
            video_url = f"https://www.youtube.com/watch?v={video_id}"

        duration = metadata.get("duration")
        if isinstance(duration, bool) or not isinstance(
            duration, (int, float)
        ):
            duration = None

        return {
            "video_id": video_id,
            "source_platform": "youtube",
            "channel_name": self._optional_string(channel_name),
            "channel_url": self._optional_string(channel_url),
            "video_url": video_url,
            "title": self._optional_string(metadata.get("title")),
            "uploader": self._optional_string(metadata.get("uploader")),
            "upload_date": self._optional_string(
                metadata.get("upload_date")
            ),
            "duration_seconds": duration,
            "discovered_at": utc_now(),
            "downloaded_at": None,
            "local_file_path": None,
            "status": VideoStatus.DISCOVERED.value,
            "error_message": None,
            "transcription": default_transcription(),
        }

    @staticmethod
    def _optional_string(value: Any) -> str | None:
        return value if isinstance(value, str) and value else None

    def _download_discovered_video(
        self, metadata: dict[str, Any], record: dict[str, Any]
    ) -> VideoStatus:
        before = self._read_archive_entries()
        completed_path: str | None = None

        def capture_path(progress: dict[str, Any]) -> None:
            nonlocal completed_path
            if progress.get("status") == "finished":
                info = progress.get("info_dict")
                path = progress.get("filepath") or progress.get("filename")
                if not isinstance(path, str) and isinstance(info, dict):
                    path = info.get("filepath") or info.get("_filename")
                if isinstance(path, str):
                    completed_path = path

        options = self._build_options()
        options["noplaylist"] = True
        options["progress_hooks"] = [capture_path]
        options["postprocessor_hooks"] = [capture_path]

        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                exit_code = downloader.download([record["video_url"]])
                if completed_path is None:
                    prepared = downloader.prepare_filename(metadata)
                    if isinstance(prepared, str):
                        completed_path = prepared
        except Exception as error:
            self._update_status(
                record,
                VideoStatus.FAILED,
                error_message=f"yt-dlp failed: {error}",
            )
            return VideoStatus.FAILED

        if exit_code != 0:
            self._update_status(
                record,
                VideoStatus.FAILED,
                error_message=f"yt-dlp exited with status {exit_code}.",
            )
            return VideoStatus.FAILED

        added_entries = self._read_archive_entries() - before
        if not added_entries:
            self._update_status(record, VideoStatus.SKIPPED)
            return VideoStatus.SKIPPED

        self._update_status(
            record,
            VideoStatus.DOWNLOADED,
            downloaded_at=utc_now(),
            local_file_path=completed_path,
        )
        return VideoStatus.DOWNLOADED

    def _update_status(
        self,
        record: dict[str, Any],
        status: VideoStatus,
        *,
        downloaded_at: str | None = None,
        local_file_path: str | None = None,
        error_message: str | None = None,
    ) -> None:
        updated = record.copy()
        updated.update(
            {
                "status": status.value,
                "downloaded_at": downloaded_at,
                "local_file_path": local_file_path,
                "error_message": error_message,
            }
        )
        self.manifest.upsert(updated)

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
