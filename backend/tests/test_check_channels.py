from unittest.mock import MagicMock, patch

import pytest

from backend.app import check_channels
from backend.services.youtube_downloader import DownloadResult, DownloadStatus


CHANNELS = [
    {"name": "One", "youtube_url": "https://youtube.com/@one", "enabled": True},
    {"name": "Two", "youtube_url": "https://youtube.com/@two", "enabled": True},
    {"name": "Three", "youtube_url": "https://youtube.com/@three", "enabled": True},
]


def run_checker(channels, results, argv=(), dry_run=False):
    manager = MagicMock()
    manager.get_enabled_channels.return_value = channels
    downloader = MagicMock()
    method = (
        downloader.discover_recent_channel_videos
        if dry_run
        else downloader.download_recent_channel_videos
    )
    method.side_effect = results

    with patch.object(
        check_channels, "ChannelManager", return_value=manager
    ), patch.object(
        check_channels, "YouTubeDownloader", return_value=downloader
    ):
        exit_code = check_channels.main(argv)

    return exit_code, downloader


def test_normal_mode_returns_zero_and_summarizes_successes_and_skips(capsys):
    results = [
        DownloadResult(DownloadStatus.SUCCESS, "one", "downloaded", 1),
        DownloadResult(DownloadStatus.SKIPPED, "two", "already downloaded"),
    ]

    exit_code, downloader = run_checker(CHANNELS[:2], results)

    assert exit_code == 0
    assert downloader.download_recent_channel_videos.call_count == 2
    assert downloader.discover_recent_channel_videos.call_count == 0
    assert "Summary: 1 successful, 1 skipped, 0 failed." in (
        capsys.readouterr().out
    )


def test_returns_nonzero_when_any_channel_fails(capsys):
    results = [
        DownloadResult(DownloadStatus.SUCCESS, "one", "downloaded", 1),
        DownloadResult(DownloadStatus.FAILED, "two", "yt-dlp failed"),
        DownloadResult(DownloadStatus.SKIPPED, "three", "already downloaded"),
    ]

    exit_code, _ = run_checker(CHANNELS, results)

    assert exit_code == 1
    assert "Summary: 1 successful, 1 skipped, 1 failed." in (
        capsys.readouterr().out
    )


def test_dry_run_uses_discovery_only_and_prints_notice_and_summary(capsys):
    results = [
        DownloadResult(DownloadStatus.SUCCESS, "one", "discovered"),
        DownloadResult(DownloadStatus.SUCCESS, "two", "discovered"),
    ]

    exit_code, downloader = run_checker(
        CHANNELS[:2], results, ("--dry-run",), dry_run=True
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert downloader.discover_recent_channel_videos.call_count == 2
    assert downloader.download_recent_channel_videos.call_count == 0
    assert "Dry-run mode is active; no media will be downloaded." in output
    assert "Summary: 2 successful, 0 skipped, 0 failed." in output


def test_dry_run_returns_nonzero_for_metadata_failure(capsys):
    result = DownloadResult(
        DownloadStatus.FAILED, "one", "Metadata collection failed"
    )

    exit_code, _ = run_checker(
        CHANNELS[:1], [result], ("--dry-run",), dry_run=True
    )

    assert exit_code == 1
    assert "Summary: 0 successful, 0 skipped, 1 failed." in (
        capsys.readouterr().out
    )


def test_treats_validation_exception_as_channel_failure(capsys):
    exit_code, _ = run_checker(CHANNELS[:1], [ValueError("bad URL")])

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "Failed to check One: bad URL" in output
    assert "Summary: 0 successful, 0 skipped, 1 failed." in output


def test_configuration_error_returns_nonzero(capsys):
    manager = MagicMock()
    manager.get_enabled_channels.side_effect = ValueError("bad config")

    with patch.object(
        check_channels, "ChannelManager", return_value=manager
    ), patch.object(check_channels, "YouTubeDownloader"):
        exit_code = check_channels.main()

    assert exit_code == 1
    assert "Configuration error: bad config" in capsys.readouterr().out


def test_no_enabled_channels_returns_zero_with_summary(capsys):
    exit_code, downloader = run_checker([], [])

    assert exit_code == 0
    downloader.download_recent_channel_videos.assert_not_called()
    assert "Summary: 0 successful, 0 skipped, 0 failed." in (
        capsys.readouterr().out
    )


def test_custom_manifest_path_is_passed_to_downloader(tmp_path):
    custom_path = tmp_path / "custom" / "videos.json"
    manager = MagicMock()
    manager.get_enabled_channels.return_value = []

    with patch.object(
        check_channels, "ChannelManager", return_value=manager
    ), patch.object(check_channels, "YouTubeDownloader") as downloader_class:
        exit_code = check_channels.main(
            ("--manifest-path", str(custom_path))
        )

    assert exit_code == 0
    downloader_class.assert_called_once_with(manifest_path=custom_path)


def test_valid_max_videos_is_forwarded():
    result = DownloadResult(DownloadStatus.SUCCESS, "one", "downloaded")

    exit_code, downloader = run_checker(
        CHANNELS[:1], [result], ("--max-videos", "7")
    )

    assert exit_code == 0
    downloader.download_recent_channel_videos.assert_called_once_with(
        channel_name="One",
        channel_url="https://youtube.com/@one",
        max_videos=7,
    )


def test_per_channel_max_videos_is_stricter_than_cli_limit():
    result = DownloadResult(DownloadStatus.SUCCESS, "caseoh", "downloaded")
    limited = [{
        "name": "CaseOh",
        "youtube_url": "https://www.youtube.com/@caseoh_",
        "enabled": True,
        "max_videos_per_cycle": 1,
    }]

    exit_code, downloader = run_checker(
        limited, [result], ("--max-videos", "7")
    )

    assert exit_code == 0
    downloader.download_recent_channel_videos.assert_called_once_with(
        channel_name="CaseOh",
        channel_url="https://www.youtube.com/@caseoh_",
        max_videos=1,
    )


@pytest.mark.parametrize("value", ["0", "-1", "three", "1.5"])
def test_invalid_max_videos_produces_argparse_error(value, capsys):
    with pytest.raises(SystemExit) as error:
        check_channels.main(("--max-videos", value))

    assert error.value.code == 2
    assert "--max-videos" in capsys.readouterr().err
