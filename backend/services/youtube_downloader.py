from pathlib import Path
import sys

import yt_dlp


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOWNLOAD_DIRECTORY = PROJECT_ROOT / "data" / "downloads"
ARCHIVE_DIRECTORY = PROJECT_ROOT / "data" / "database"


def download_video(video_url: str) -> None:
    """Download one YouTube video into the project downloads folder."""

    DOWNLOAD_DIRECTORY.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIRECTORY.mkdir(parents=True, exist_ok=True)

    options = {
        "outtmpl": str(
            DOWNLOAD_DIRECTORY
            / "%(uploader)s"
            / "%(upload_date)s_%(id)s_%(title)s.%(ext)s"
        ),
        "format": "bestvideo*+bestaudio/best",
        "merge_output_format": "mp4",
        "restrictfilenames": True,
        "noplaylist": True,
        "download_archive": str(
            ARCHIVE_DIRECTORY / "downloaded_videos.txt"
        ),
    }

    print(f"Downloading to: {DOWNLOAD_DIRECTORY}")

    with yt_dlp.YoutubeDL(options) as downloader:
        downloader.download([video_url])


def main() -> None:
    if len(sys.argv) != 2:
        print(
            "Usage: python backend/services/youtube_downloader.py "
            '"YOUTUBE_VIDEO_URL"'
        )
        raise SystemExit(1)

    video_url = sys.argv[1]

    try:
        download_video(video_url)
    except yt_dlp.utils.DownloadError as error:
        print(f"Download failed: {error}")
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
