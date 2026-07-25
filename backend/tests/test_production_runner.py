from __future__ import annotations

import io
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.services.clip_candidate_generator import AnalysisResultStatus
from backend.services.production_runner import (
    ProductionDependencies, ProductionLock, ProductionLogger,
    ProductionRunLocked, ProductionRunner, ProductionState,
    _read_review_video_ids, main,
)
from backend.services.video_manifest import VideoStatus
from backend.services.video_transcriber import TranscriptionResultStatus
from backend.services.youtube_downloader import (
    ChannelDiscoveryResult, DownloadResult, DownloadStatus,
)


def metadata(video_id: str) -> dict:
    return {
        "id": video_id, "title": f"Video {video_id}",
        "webpage_url": f"https://www.youtube.com/watch?v={video_id}",
    }


class FakeChannels:
    def __init__(self, names=("Creator A",)):
        self.names = names

    def get_enabled_channels(self):
        return [
            {"name": name, "youtube_url": f"https://www.youtube.com/@{name.replace(' ', '')}"}
            for name in self.names
        ]


class FakeDiscovery:
    def __init__(self, entries=None, failures=()):
        self.entries = entries or {}
        self.failures = set(failures)
        self.calls = []

    def discover_recent_channel_metadata(self, name, url, maximum):
        self.calls.append((name, url, maximum))
        if name in self.failures:
            return ChannelDiscoveryResult(
                DownloadStatus.FAILED, name, url, (), "discovery failed"
            )
        values = self.entries.get(name, [metadata(f"{name}-video")])
        return ChannelDiscoveryResult(
            DownloadStatus.SUCCESS, name, url, tuple(values),
            f"Discovered {len(values)} recent video(s).",
        )


class FakeManifest:
    def __init__(self, root: Path):
        self.root = root
        self.records = {}

    def get(self, video_id):
        return self.records.get(video_id)


class FakeDownloader:
    def __init__(self, manifest, failures=()):
        self.manifest = manifest
        self.failures = set(failures)
        self.calls = []

    def download_discovered_entry(self, item, creator, url):
        video_id = item["id"]
        self.calls.append(video_id)
        if video_id in self.failures:
            return DownloadResult(
                DownloadStatus.FAILED, item["webpage_url"], "download failed"
            )
        media = self.manifest.root / f"{video_id}.mp4"
        media.write_bytes(b"local media")
        self.manifest.records[video_id] = {
            "video_id": video_id, "status": VideoStatus.DOWNLOADED.value,
            "local_file_path": str(media),
        }
        return DownloadResult(
            DownloadStatus.SUCCESS, item["webpage_url"], "downloaded", 1
        )


class FakeTranscriber:
    def __init__(self, failures=()):
        self.failures = set(failures)
        self.calls = []

    def transcribe(self, *, video_id, retry_failed):
        self.calls.append((video_id, retry_failed))
        failed = video_id in self.failures
        return SimpleNamespace(results=[SimpleNamespace(
            video_id=video_id,
            status=(
                TranscriptionResultStatus.FAILED if failed
                else TranscriptionResultStatus.SUCCESS
            ),
            message="transcription failed" if failed else "transcribed",
        )])


class FakeAnalyzer:
    def __init__(self, failures=()):
        self.failures = set(failures)
        self.calls = []

    def analyze(self, *, video_id):
        self.calls.append(video_id)
        failed = video_id in self.failures
        return SimpleNamespace(results=[SimpleNamespace(
            video_id=video_id,
            status=AnalysisResultStatus.FAILED if failed else AnalysisResultStatus.SUCCESS,
            message="analysis failed" if failed else "analyzed",
        )])


class FakeRenderer:
    def __init__(self, queue, failures=()):
        self.queue = queue
        self.failures = set(failures)
        self.calls = []

    def render(self, video_id, **kwargs):
        self.calls.append((video_id, kwargs))
        if video_id in self.failures:
            return SimpleNamespace(
                successful=0, skipped=0, failed=1,
                items=(SimpleNamespace(status="failed", message="render failed"),),
            )
        items = []
        for rank in range(1, 3):
            self.queue.items.append({
                "video_id": video_id, "review_id": f"review-{video_id}-{rank}"
            })
            items.append(SimpleNamespace(status="success", message="rendered"))
        return SimpleNamespace(successful=2, skipped=0, failed=0, items=tuple(items))


class FakeQueue:
    def __init__(self, items=()):
        self.items = list(items)

    def list_items(self):
        return list(self.items)


def setup_runner(
    tmp_path, *, channels=("Creator A",), entries=None, discovery_failures=(),
    download_failures=(), transcription_failures=(), render_failures=(),
    existing_queue=(), dry_run=False,
):
    manifest = FakeManifest(tmp_path)
    queue = FakeQueue(existing_queue)
    discovery = FakeDiscovery(entries, discovery_failures)
    downloader = FakeDownloader(manifest, download_failures)
    transcriber = FakeTranscriber(transcription_failures)
    analyzer = FakeAnalyzer()
    renderer = FakeRenderer(queue, render_failures)
    dependencies = ProductionDependencies(
        FakeChannels(channels), discovery, downloader, manifest,
        transcriber, analyzer, renderer, queue,
    )
    output = io.StringIO()
    state = ProductionState(tmp_path / "production" / "state.json")
    runner = ProductionRunner(
        dependencies, state=state, lock_factory=lambda: nullcontext(),
        logger=ProductionLogger(tmp_path / "production.log", dry_run=dry_run, stream=output),
        dry_run=dry_run,
    )
    return runner, dependencies, state, output


def test_new_video_discovery_success_and_review_queue_creation(tmp_path):
    runner, dependencies, state, output = setup_runner(
        tmp_path, entries={"Creator A": [metadata("new-video")]}
    )
    summary = runner.run()
    assert summary.creators_checked == 1
    assert summary.new_videos_discovered == 1
    assert summary.videos_processed == 1
    assert summary.previews_created == 2 and summary.failures == 0
    assert [item["video_id"] for item in dependencies.review_queue.items] == [
        "new-video", "new-video"
    ]
    stored = state.load()["videos"]["new-video"]
    assert stored["status"] == "completed" and stored["stage"] == "review_ready"
    assert stored["preview_count"] == 2 and stored["attempts"] == 1
    assert '"event": "run_summary"' in output.getvalue()


def test_already_processed_state_and_review_queue_are_deduplicated(tmp_path):
    runner, dependencies, state, _ = setup_runner(
        tmp_path,
        entries={"Creator A": [metadata("state-done"), metadata("queue-done")]},
        existing_queue=({"video_id": "queue-done", "review_id": "review-existing"},),
    )
    document = state.empty()
    state.begin(document, "state-done", "Creator A")
    state.update(document, "state-done", stage="review_ready", status="completed")
    summary = runner.run()
    assert summary.videos_skipped == 2 and summary.videos_processed == 0
    assert not dependencies.downloader.calls


def test_dry_run_discovers_and_plans_without_writes(tmp_path):
    runner, dependencies, state, output = setup_runner(
        tmp_path, entries={"Creator A": [metadata("planned")]}, dry_run=True
    )
    summary = runner.run()
    assert summary.dry_run and summary.new_videos_discovered == 1
    assert not state.path.exists()
    assert not (tmp_path / "production.log").exists()
    assert not dependencies.downloader.calls
    assert not dependencies.transcriber.calls
    assert not dependencies.renderer.calls
    assert '"event": "video_planned"' in output.getvalue()


def test_dry_run_read_only_review_deduplication(tmp_path):
    queue = tmp_path / "reviews.json"
    queue.write_text(json.dumps({
        "version": 3, "updated_at": "unused",
        "items": [{"video_id": "already-reviewed"}],
    }), encoding="utf-8")
    assert _read_review_video_ids(queue) == frozenset({"already-reviewed"})


def test_creator_and_video_failures_are_isolated(tmp_path):
    runner, dependencies, state, _ = setup_runner(
        tmp_path, channels=("Broken Creator", "Creator A"),
        discovery_failures=("Broken Creator",),
        entries={"Creator A": [metadata("bad"), metadata("good")]},
        download_failures=("bad",),
    )
    summary = runner.run()
    assert summary.creators_checked == 2 and summary.failures == 2
    assert summary.videos_processed == 1 and summary.previews_created == 2
    assert state.load()["videos"]["bad"]["status"] == "failed"
    assert state.load()["videos"]["good"]["status"] == "completed"
    assert dependencies.downloader.calls == ["bad", "good"]


def test_failed_and_interrupted_runs_are_recoverable(tmp_path):
    runner, _, state, _ = setup_runner(
        tmp_path, entries={"Creator A": [metadata("retry")]}
    )
    document = state.empty()
    state.begin(document, "retry", "Creator A")
    summary = runner.run()
    assert summary.videos_processed == 1
    stored = state.load()["videos"]["retry"]
    assert stored["status"] == "completed" and stored["attempts"] == 2


def test_failed_state_retries_even_when_partial_review_item_exists(tmp_path):
    runner, _, state, _ = setup_runner(
        tmp_path, entries={"Creator A": [metadata("partial")]},
        existing_queue=({"video_id": "partial", "review_id": "review-partial"},),
    )
    document = state.empty()
    state.begin(document, "partial", "Creator A")
    state.update(
        document, "partial", stage="preview_rendering", status="failed",
        error="one preview failed", preview_count=1,
    )
    summary = runner.run()
    assert summary.videos_processed == 1
    assert state.load()["videos"]["partial"]["status"] == "completed"


def test_state_is_atomic_and_corruption_is_actionable(tmp_path):
    state = ProductionState(tmp_path / "state.json")
    document = state.empty()
    state.begin(document, "video", "Creator")
    assert state.load()["videos"]["video"]["status"] == "processing"
    assert not list(tmp_path.glob(".state.json.*.tmp"))
    state.path.write_text("{", encoding="utf-8")
    with pytest.raises(Exception, match="corrupt"):
        state.load()


def test_overlapping_run_lock_is_rejected(tmp_path):
    path = tmp_path / "production.lock"
    with ProductionLock(path):
        with pytest.raises(ProductionRunLocked, match="Another production run"):
            with ProductionLock(path):
                pass


def test_reuses_existing_downloaded_media(tmp_path):
    runner, dependencies, _, _ = setup_runner(
        tmp_path, entries={"Creator A": [metadata("local")]}
    )
    media = tmp_path / "local.mp4"
    media.write_bytes(b"existing")
    dependencies.manifest.records["local"] = {
        "video_id": "local", "status": "downloaded",
        "local_file_path": str(media),
    }
    summary = runner.run()
    assert summary.videos_processed == 1
    assert not dependencies.downloader.calls


def test_cli_explicit_argv_and_invalid_state_exit(tmp_path, monkeypatch):
    state = tmp_path / "bad.json"
    state.write_text("{", encoding="utf-8")
    monkeypatch.setattr(
        "backend.services.production_runner.create_dependencies",
        lambda args: ProductionDependencies(FakeChannels(()), FakeDiscovery()),
    )
    assert main([
        "--dry-run", "--state-path", str(state),
        "--lock-path", str(tmp_path / "lock"),
        "--log-path", str(tmp_path / "log"),
    ]) == 1
