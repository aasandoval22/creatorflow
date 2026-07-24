from unittest.mock import MagicMock, patch

from backend.app import check_channels
from backend.services.youtube_downloader import DownloadResult, DownloadStatus


CHANNELS = [
    {"name": "One", "youtube_url": "https://youtube.com/@one", "enabled": True},
    {"name": "Two", "youtube_url": "https://youtube.com/@two", "enabled": True},
    {"name": "Three", "youtube_url": "https://youtube.com/@three", "enabled": True},
]


def run_checker(channels, results):
    manager = MagicMock()
    manager.get_enabled_channels.return_value = channels
    downloader = MagicMock()
    downloader.download_recent_channel_videos.side_effect = results

    with patch.object(check_channels, "ChannelManager", return_value=manager), patch.object(
        check_channels, "YouTubeDownloader", return_value=downloader
    ):
        exit_code = check_channels.main()

    return exit_code, downloader


def test_returns_zero_and_summarizes_successes_and_skips(capsys):
    results = [
        DownloadResult(DownloadStatus.SUCCESS, "one", "downloaded", 1),
        DownloadResult(DownloadStatus.SKIPPED, "two", "already downloaded"),
    ]

    exit_code, downloader = run_checker(CHANNELS[:2], results)

    assert exit_code == 0
    assert downloader.download_recent_channel_videos.call_count == 2
    assert "Summary: 1 successful, 1 skipped, 0 failed." in capsys.readouterr().out


def test_returns_nonzero_when_any_channel_fails(capsys):
    results = [
        DownloadResult(DownloadStatus.SUCCESS, "one", "downloaded", 1),
        DownloadResult(DownloadStatus.FAILED, "two", "yt-dlp failed"),
        DownloadResult(DownloadStatus.SKIPPED, "three", "already downloaded"),
    ]

    exit_code, _ = run_checker(CHANNELS, results)

    assert exit_code == 1
    assert "Summary: 1 successful, 1 skipped, 1 failed." in capsys.readouterr().out


def test_treats_validation_exception_as_channel_failure(capsys):
    exit_code, _ = run_checker(CHANNELS[:1], [ValueError("bad URL")])

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "Failed to check One: bad URL" in output
    assert "Summary: 0 successful, 0 skipped, 1 failed." in output


def test_configuration_error_returns_nonzero(capsys):
    manager = MagicMock()
    manager.get_enabled_channels.side_effect = ValueError("bad config")

    with patch.object(check_channels, "ChannelManager", return_value=manager), patch.object(
        check_channels, "YouTubeDownloader"
    ):
        exit_code = check_channels.main()

    assert exit_code == 1
    assert "Configuration error: bad config" in capsys.readouterr().out


def test_no_enabled_channels_returns_zero_with_summary(capsys):
    exit_code, downloader = run_checker([], [])

    assert exit_code == 0
    downloader.download_recent_channel_videos.assert_not_called()
    assert "Summary: 0 successful, 0 skipped, 0 failed." in capsys.readouterr().out
