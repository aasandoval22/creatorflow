from unittest.mock import MagicMock, patch

import pytest

from backend.services.video_manifest import VideoStatus
from backend.services.youtube_downloader import (
    DownloadResult, DownloadStatus, YouTubeDownloader, find_deno_executable,
)


VIDEO_URL = "https://www.youtube.com/watch?v=abc"
CHANNEL_URL = "https://www.youtube.com/@creator"
VIDEOS_URL = f"{CHANNEL_URL}/videos"


def metadata(video_id="abc", **overrides):
    value = {
        "id": video_id,
        "webpage_url": f"https://www.youtube.com/watch?v={video_id}",
        "title": "A video",
        "uploader": "Creator",
        "upload_date": "20260724",
        "duration": 42,
    }
    value.update(overrides)
    return value


def executable_deno(tmp_path):
    path = tmp_path / ".deno" / "bin" / "deno"
    path.parent.mkdir(parents=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o700)
    return path


@pytest.fixture
def downloader(tmp_path):
    return YouTubeDownloader(
        tmp_path / "downloads",
        tmp_path / "archive",
        tmp_path / "manifests" / "videos.json",
        deno_path=executable_deno(tmp_path),
    )


def youtube_dl_instances(metadata_value, download_action=0, filename=None):
    metadata_instance = MagicMock()
    metadata_instance.__enter__.return_value = metadata_instance
    if isinstance(metadata_value, BaseException):
        metadata_instance.extract_info.side_effect = metadata_value
    else:
        metadata_instance.extract_info.return_value = metadata_value

    download_instance = MagicMock()
    download_instance.__enter__.return_value = download_instance
    if callable(download_action) or isinstance(download_action, BaseException):
        download_instance.download.side_effect = download_action
    else:
        download_instance.download.return_value = download_action
    download_instance.prepare_filename.return_value = filename

    mocked = patch(
        "backend.services.youtube_downloader.yt_dlp.YoutubeDL",
        side_effect=[metadata_instance, download_instance],
    )
    return mocked, metadata_instance, download_instance


def test_detects_existing_explicit_deno_executable(tmp_path, monkeypatch):
    deno = executable_deno(tmp_path)
    monkeypatch.setenv("PATH", "")
    assert find_deno_executable(deno) == deno


def test_options_explicitly_configure_deno_for_metadata_and_download(tmp_path):
    deno = executable_deno(tmp_path)
    downloader = YouTubeDownloader(discovery_only=True, deno_path=deno)
    expected = {"deno": {"path": str(deno)}}
    assert downloader._metadata_options()["js_runtimes"] == expected
    assert downloader._build_options()["js_runtimes"] == expected


def test_standard_deno_path_works_without_interactive_path(tmp_path, monkeypatch):
    deno = executable_deno(tmp_path)
    monkeypatch.setattr(
        "backend.services.youtube_downloader.DEFAULT_DENO_PATH", deno
    )
    monkeypatch.delenv("AUTOCLIP_DENO_PATH", raising=False)
    monkeypatch.setattr(
        "backend.services.youtube_downloader.shutil.which", lambda _name: None
    )
    downloader = YouTubeDownloader(discovery_only=True)
    assert downloader.deno_path == deno
    assert downloader._metadata_options()["js_runtimes"]["deno"]["path"] == str(deno)


def test_missing_deno_warns_and_preserves_yt_dlp_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "backend.services.youtube_downloader.DEFAULT_DENO_PATH",
        tmp_path / "missing-deno",
    )
    monkeypatch.delenv("AUTOCLIP_DENO_PATH", raising=False)
    monkeypatch.setattr(
        "backend.services.youtube_downloader.shutil.which", lambda _name: None
    )
    with pytest.warns(RuntimeWarning, match="Deno JavaScript runtime was not found"):
        downloader = YouTubeDownloader(discovery_only=True)
    assert downloader.deno_path is None
    assert "js_runtimes" not in downloader._metadata_options()
    assert "js_runtimes" not in downloader._build_options()


def test_uses_injected_directories_and_preserves_archive_options(downloader):
    options = downloader._build_options()

    assert str(downloader.download_directory) in options["outtmpl"]
    assert options["download_archive"] == str(downloader.download_archive)
    assert options["ignoreerrors"] is False


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://youtube.com/@creator", VIDEOS_URL),
        ("https://youtube.com/@creator/", VIDEOS_URL),
        ("https://youtube.com/@creator/videos", VIDEOS_URL),
        ("https://youtube.com/@creator/?view=0#top", VIDEOS_URL),
    ],
)
def test_normalizes_channel_video_url(downloader, url, expected):
    playlist = {"entries": [metadata()]}
    mocked, metadata_instance, _ = youtube_dl_instances(playlist)

    with mocked:
        result = downloader.download_recent_channel_videos("Creator", url)

    metadata_instance.extract_info.assert_called_once_with(
        expected, download=False
    )
    assert result.status is DownloadStatus.SKIPPED


def test_collects_metadata_before_download_and_records_discovered(downloader):
    events = []
    original_upsert = downloader.manifest.upsert

    def track_upsert(record):
        events.append(("manifest", record["status"]))
        return original_upsert(record)

    def download(_urls):
        events.append(("download", None))
        return 0

    downloader.manifest.upsert = track_upsert
    mocked, metadata_instance, _ = youtube_dl_instances(metadata(), download)

    with mocked:
        downloader.download_video(VIDEO_URL)

    metadata_instance.extract_info.assert_called_once_with(
        VIDEO_URL, download=False
    )
    assert events[0] == ("manifest", VideoStatus.DISCOVERED.value)
    assert events[1] == ("download", None)


def test_read_only_metadata_discovery_does_not_create_storage_or_manifest(tmp_path):
    downloads = tmp_path / "downloads"
    archive = tmp_path / "archive"
    discovery = YouTubeDownloader(
        downloads, archive, tmp_path / "manifest.json", discovery_only=True
    )
    metadata_instance = MagicMock()
    metadata_instance.__enter__.return_value = metadata_instance
    metadata_instance.extract_info.return_value = {
        "entries": [metadata("one"), metadata("two")]
    }
    with patch(
        "backend.services.youtube_downloader.yt_dlp.YoutubeDL",
        return_value=metadata_instance,
    ):
        result = discovery.discover_recent_channel_metadata(
            "Creator", CHANNEL_URL, 2
        )
    assert result.status is DownloadStatus.SUCCESS
    assert [entry["id"] for entry in result.entries] == ["one", "two"]
    assert not downloads.exists() and not archive.exists()
    assert discovery.manifest is None


def test_download_discovered_entry_reuses_single_entry_pipeline(downloader):
    item = metadata()
    with patch.object(
        downloader, "_process_entries",
        return_value=DownloadResult(
            DownloadStatus.SUCCESS, VIDEO_URL, "downloaded", 1
        ),
    ) as process:
        result = downloader.download_discovered_entry(
            item, "Creator", CHANNEL_URL
        )
    assert result.status is DownloadStatus.SUCCESS
    process.assert_called_once_with(
        [item], VIDEO_URL, "Creator", CHANNEL_URL, download=True
    )


def test_success_updates_downloaded_path_and_timestamp(downloader):
    output_path = str(downloader.download_directory / "Creator" / "abc.mp4")

    def download(_urls):
        downloader.download_archive.write_text(
            "youtube abc\n", encoding="utf-8"
        )
        return 0

    mocked, _, _ = youtube_dl_instances(metadata(), download, output_path)
    with mocked:
        result = downloader.download_video(VIDEO_URL)

    saved = downloader.manifest.get("abc")
    assert result.status is DownloadStatus.SUCCESS
    assert result.downloaded_count == 1
    assert saved["status"] == VideoStatus.DOWNLOADED.value
    assert saved["local_file_path"] == output_path
    assert saved["downloaded_at"].endswith("+00:00")
    assert saved["error_message"] is None


def test_preserves_archive_and_marks_previously_downloaded_as_skipped(
    downloader,
):
    downloader.download_archive.write_text(
        "youtube abc\n", encoding="utf-8"
    )
    mocked, _, _ = youtube_dl_instances(metadata())

    with mocked:
        result = downloader.download_video(VIDEO_URL)

    assert result.status is DownloadStatus.SKIPPED
    assert downloader.manifest.get("abc")["status"] == "skipped"
    assert (
        downloader.download_archive.read_text(encoding="utf-8")
        == "youtube abc\n"
    )


@pytest.mark.parametrize(
    ("download_action", "message"),
    [
        (1, "status 1"),
        (RuntimeError("offline failure"), "offline failure"),
    ],
)
def test_download_failure_becomes_failed_record(
    downloader, download_action, message
):
    mocked, _, _ = youtube_dl_instances(metadata(), download_action)

    with mocked:
        result = downloader.download_video(VIDEO_URL)

    saved = downloader.manifest.get("abc")
    assert result.status is DownloadStatus.FAILED
    assert saved["status"] == "failed"
    assert message in saved["error_message"]


def test_metadata_failure_returns_failure_without_download(downloader):
    metadata_instance = MagicMock()
    metadata_instance.__enter__.return_value = metadata_instance
    metadata_instance.extract_info.side_effect = RuntimeError(
        "metadata offline"
    )

    with patch(
        "backend.services.youtube_downloader.yt_dlp.YoutubeDL",
        return_value=metadata_instance,
    ) as youtube_dl:
        result = downloader.download_video(VIDEO_URL)

    assert result.status is DownloadStatus.FAILED
    assert "metadata offline" in result.message
    assert youtube_dl.call_count == 1
    saved = downloader.manifest.get("abc")
    assert saved["status"] == "failed"
    assert "metadata offline" in saved["error_message"]


def test_metadata_failure_without_known_id_does_not_create_record(downloader):
    metadata_instance = MagicMock()
    metadata_instance.__enter__.return_value = metadata_instance
    metadata_instance.extract_info.side_effect = RuntimeError("offline")

    with patch(
        "backend.services.youtube_downloader.yt_dlp.YoutubeDL",
        return_value=metadata_instance,
    ):
        result = downloader.download_video("https://youtube.com/video")

    assert result.status is DownloadStatus.FAILED
    assert downloader.manifest.read_records() == []


def test_missing_optional_metadata_values_are_recorded_as_null(downloader):
    minimal_metadata = {"id": "abc"}
    metadata_instance = MagicMock()
    metadata_instance.__enter__.return_value = metadata_instance
    metadata_instance.extract_info.return_value = minimal_metadata

    with patch(
        "backend.services.youtube_downloader.yt_dlp.YoutubeDL",
        return_value=metadata_instance,
    ):
        result = downloader.discover_recent_channel_videos(
            "Creator", CHANNEL_URL
        )

    saved = downloader.manifest.get("abc")
    assert result.status is DownloadStatus.SUCCESS
    assert saved["title"] is None
    assert saved["uploader"] is None
    assert saved["upload_date"] is None
    assert saved["duration_seconds"] is None
    assert saved["video_url"].endswith("watch?v=abc")


def test_dry_run_discovers_metadata_without_download_or_archive(downloader):
    playlist = {"entries": [metadata()]}
    metadata_instance = MagicMock()
    metadata_instance.__enter__.return_value = metadata_instance
    metadata_instance.extract_info.return_value = playlist

    with patch(
        "backend.services.youtube_downloader.yt_dlp.YoutubeDL",
        return_value=metadata_instance,
    ) as youtube_dl:
        result = downloader.discover_recent_channel_videos(
            "Creator", CHANNEL_URL, 2
        )

    assert result.status is DownloadStatus.SUCCESS
    assert downloader.manifest.get("abc")["status"] == "discovered"
    assert not downloader.download_archive.exists()
    assert youtube_dl.call_count == 1
    options = youtube_dl.call_args.args[0]
    assert options["skip_download"] is True
    assert "download_archive" not in options


def test_accepts_iterable_playlist_entries(downloader):
    playlist = {"entries": iter([metadata()])}
    metadata_instance = MagicMock()
    metadata_instance.__enter__.return_value = metadata_instance
    metadata_instance.extract_info.return_value = playlist

    with patch(
        "backend.services.youtube_downloader.yt_dlp.YoutubeDL",
        return_value=metadata_instance,
    ):
        result = downloader.discover_recent_channel_videos(
            "Creator", CHANNEL_URL
        )

    assert result.status is DownloadStatus.SUCCESS
    assert downloader.manifest.get("abc")["status"] == "discovered"


def test_rejects_invalid_max_videos_without_calling_yt_dlp(downloader):
    with patch(
        "backend.services.youtube_downloader.yt_dlp.YoutubeDL"
    ) as youtube_dl:
        with pytest.raises(ValueError, match="at least 1"):
            downloader.download_recent_channel_videos(
                "Creator", CHANNEL_URL, 0
            )

    youtube_dl.assert_not_called()
