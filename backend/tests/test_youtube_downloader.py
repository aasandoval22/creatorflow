from unittest.mock import MagicMock, patch

import pytest

from backend.services.youtube_downloader import DownloadStatus, YouTubeDownloader


@pytest.fixture
def downloader(tmp_path):
    return YouTubeDownloader(tmp_path / "downloads", tmp_path / "archive")


def youtube_dl_mock(download_action):
    instance = MagicMock()
    instance.__enter__.return_value = instance
    if callable(download_action) or isinstance(download_action, BaseException):
        instance.download.side_effect = download_action
    else:
        instance.download.return_value = download_action
    return patch("backend.services.youtube_downloader.yt_dlp.YoutubeDL", return_value=instance), instance


def test_uses_injected_directories(downloader):
    options = downloader._build_options()

    assert str(downloader.download_directory) in options["outtmpl"]
    assert options["download_archive"] == str(downloader.download_archive)
    assert options["ignoreerrors"] is False


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://youtube.com/@creator", "https://www.youtube.com/@creator/videos"),
        ("https://youtube.com/@creator/", "https://www.youtube.com/@creator/videos"),
        ("https://youtube.com/@creator/videos", "https://www.youtube.com/@creator/videos"),
        ("https://youtube.com/@creator/?view=0#top", "https://www.youtube.com/@creator/videos"),
    ],
)
def test_normalizes_channel_video_url(downloader, url, expected):
    mock_patch, instance = youtube_dl_mock(0)
    with mock_patch:
        result = downloader.download_recent_channel_videos("Creator", url)

    instance.download.assert_called_once_with([expected])
    assert result.status is DownloadStatus.SKIPPED


def test_returns_success_when_archive_gains_entries(downloader):
    def download(_urls):
        downloader.download_archive.write_text("youtube abc\n", encoding="utf-8")
        return 0

    mock_patch, _ = youtube_dl_mock(download)
    with mock_patch:
        result = downloader.download_video("https://youtube.com/watch?v=abc")

    assert result.status is DownloadStatus.SUCCESS
    assert result.downloaded_count == 1


def test_preserves_archive_and_returns_skipped(downloader):
    downloader.download_archive.write_text("youtube abc\n", encoding="utf-8")
    mock_patch, _ = youtube_dl_mock(0)

    with mock_patch:
        result = downloader.download_video("https://youtube.com/watch?v=abc")

    assert result.status is DownloadStatus.SKIPPED
    assert downloader.download_archive.read_text(encoding="utf-8") == "youtube abc\n"


def test_returns_failed_for_nonzero_exit(downloader):
    mock_patch, _ = youtube_dl_mock(1)

    with mock_patch:
        result = downloader.download_video("https://youtube.com/watch?v=bad")

    assert result.status is DownloadStatus.FAILED
    assert "status 1" in result.message


def test_returns_failed_for_yt_dlp_exception(downloader):
    mock_patch, _ = youtube_dl_mock(RuntimeError("offline failure"))

    with mock_patch:
        result = downloader.download_video("https://youtube.com/watch?v=bad")

    assert result.status is DownloadStatus.FAILED
    assert "offline failure" in result.message


def test_rejects_invalid_max_videos_without_calling_yt_dlp(downloader):
    with patch("backend.services.youtube_downloader.yt_dlp.YoutubeDL") as youtube_dl:
        with pytest.raises(ValueError, match="at least 1"):
            downloader.download_recent_channel_videos("Creator", "https://youtube.com/@creator", 0)

    youtube_dl.assert_not_called()
