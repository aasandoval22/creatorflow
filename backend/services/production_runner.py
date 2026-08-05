"""Dependable manual ingestion-to-review production orchestration."""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tempfile
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, TextIO

try:
    import fcntl
except ImportError:  # pragma: no cover - production host is Linux
    fcntl = None

from backend.app.analyze_clips import positive_integer
from backend.services.batch_preview_renderer import BatchPreviewRenderer
from backend.services.channel_manager import (
    CONFIG_FILE,
    ChannelManager,
    channel_video_limit,
)
from backend.services.clip_candidate_generator import (
    DEFAULT_CANDIDATE_DIRECTORY, AnalysisResultStatus, ClipCandidateGenerator,
)
from backend.services.clip_context_expander import ContextExpansionConfiguration
from backend.services.clip_review_queue import (
    DEFAULT_REVIEW_QUEUE_PATH, ClipReviewQueue,
)
from backend.services.video_manifest import (
    DEFAULT_MANIFEST_PATH, TranscriptionStatus, VideoManifest, VideoStatus, utc_now,
)
from backend.services.video_preview_renderer import (
    DEFAULT_PREVIEW_DIRECTORY, CaptionConfiguration, RenderConfiguration,
    VideoPreviewRenderer,
)
from backend.services.video_transcriber import (
    DEFAULT_TRANSCRIPT_DIRECTORY, TranscriptionResultStatus, VideoTranscriber,
)
from backend.services.youtube_downloader import (
    ARCHIVE_DIRECTORY, DOWNLOAD_DIRECTORY, DownloadStatus, YouTubeDownloader,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PRODUCTION_DIRECTORY = PROJECT_ROOT / "data" / "production"
DEFAULT_STATE_PATH = DEFAULT_PRODUCTION_DIRECTORY / "processing_state.json"
DEFAULT_LOCK_PATH = DEFAULT_PRODUCTION_DIRECTORY / "production.lock"
DEFAULT_LOG_PATH = PROJECT_ROOT / "data" / "logs" / "production.jsonl"
STATE_VERSION = 1
FINAL_STATES = frozenset({"completed"})


class ProductionRunnerError(RuntimeError):
    pass


class ProductionRunLocked(ProductionRunnerError):
    pass


@dataclass
class ProductionSummary:
    creators_checked: int = 0
    new_videos_discovered: int = 0
    videos_skipped: int = 0
    videos_processed: int = 0
    previews_created: int = 0
    failures: int = 0
    dry_run: bool = False


class ProductionLogger:
    """Emit JSON lines to stdout and, for real runs, an ignored local file."""

    def __init__(
        self, path: Path = DEFAULT_LOG_PATH, *, dry_run: bool = False,
        stream: TextIO = sys.stdout,
    ) -> None:
        self.path = Path(path)
        self.dry_run = dry_run
        self.stream = stream

    def emit(self, event: str, **fields: Any) -> None:
        document = {"timestamp": utc_now(), "event": event, **fields}
        line = json.dumps(document, sort_keys=True, ensure_ascii=False)
        print(line, file=self.stream, flush=True)
        if self.dry_run:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as log:
            log.write(line + "\n")
            log.flush()


class ProductionState:
    """Strict, atomically written per-video production lifecycle state."""

    def __init__(self, path: Path = DEFAULT_STATE_PATH) -> None:
        self.path = Path(path)

    @staticmethod
    def empty() -> dict[str, Any]:
        return {"version": STATE_VERSION, "updated_at": utc_now(), "videos": {}}

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self.empty()
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ProductionRunnerError(
                f"Production state {self.path} is corrupt: {error}. "
                "Repair or move it before retrying."
            ) from error
        except OSError as error:
            raise ProductionRunnerError(
                f"Cannot read production state {self.path}: {error}."
            ) from error
        self.validate(document)
        return document

    @staticmethod
    def validate(document: Any) -> None:
        if (
            not isinstance(document, dict)
            or set(document) != {"version", "updated_at", "videos"}
            or document["version"] != STATE_VERSION
            or not isinstance(document["updated_at"], str)
            or not isinstance(document["videos"], dict)
        ):
            raise ProductionRunnerError(
                "Production state must contain version 1, updated_at, and videos."
            )
        fields = {
            "video_id", "creator", "status", "stage", "attempts",
            "first_seen_at", "updated_at", "completed_at", "last_error",
            "preview_count",
        }
        for video_id, value in document["videos"].items():
            if (
                not isinstance(video_id, str) or not video_id
                or not isinstance(value, dict) or set(value) != fields
                or value["video_id"] != video_id
                or value["status"] not in {"processing", "completed", "failed"}
                or not isinstance(value["stage"], str)
                or isinstance(value["attempts"], bool)
                or not isinstance(value["attempts"], int) or value["attempts"] < 1
                or isinstance(value["preview_count"], bool)
                or not isinstance(value["preview_count"], int)
                or value["preview_count"] < 0
            ):
                raise ProductionRunnerError(
                    f"Production state entry {video_id!r} is malformed."
                )
            for name in ("creator", "first_seen_at", "updated_at"):
                if not isinstance(value[name], str) or not value[name]:
                    raise ProductionRunnerError(
                        f"Production state entry {video_id!r} field {name!r} is invalid."
                    )
            for name in ("completed_at", "last_error"):
                if value[name] is not None and not isinstance(value[name], str):
                    raise ProductionRunnerError(
                        f"Production state entry {video_id!r} field {name!r} is invalid."
                    )

    def write(self, document: dict[str, Any]) -> None:
        candidate = copy.deepcopy(document)
        candidate["updated_at"] = utc_now()
        self.validate(candidate)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.path.parent,
                prefix=f".{self.path.name}.", suffix=".tmp", delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(candidate, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def recover_interrupted(self, document: dict[str, Any]) -> int:
        recovered = 0
        for entry in document["videos"].values():
            if entry["status"] == "processing":
                entry.update(
                    status="failed", stage="interrupted",
                    last_error="Previous production run ended before completion.",
                    updated_at=utc_now(), completed_at=None,
                )
                recovered += 1
        return recovered

    def begin(
        self, document: dict[str, Any], video_id: str, creator: str
    ) -> dict[str, Any]:
        now = utc_now()
        previous = document["videos"].get(video_id)
        entry = {
            "video_id": video_id, "creator": creator, "status": "processing",
            "stage": "download", "attempts": (previous or {}).get("attempts", 0) + 1,
            "first_seen_at": (previous or {}).get("first_seen_at", now),
            "updated_at": now, "completed_at": None, "last_error": None,
            "preview_count": (previous or {}).get("preview_count", 0),
        }
        document["videos"][video_id] = entry
        self.write(document)
        return entry

    def update(
        self, document: dict[str, Any], video_id: str, *, stage: str,
        status: str = "processing", error: str | None = None,
        preview_count: int | None = None,
    ) -> None:
        entry = document["videos"][video_id]
        entry.update(
            stage=stage, status=status, last_error=error, updated_at=utc_now(),
            completed_at=utc_now() if status == "completed" else None,
        )
        if preview_count is not None:
            entry["preview_count"] = preview_count
        self.write(document)


class ProductionLock(AbstractContextManager["ProductionLock"]):
    def __init__(self, path: Path = DEFAULT_LOCK_PATH) -> None:
        self.path = Path(path)
        self._stream: Any | None = None

    def __enter__(self) -> "ProductionLock":
        if fcntl is None:
            raise ProductionRunnerError("Production locking requires fcntl on this host.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self._stream.close()
            self._stream = None
            raise ProductionRunLocked(
                f"Another production run holds {self.path}."
            ) from error
        self._stream.seek(0)
        self._stream.truncate()
        self._stream.write(f"pid={os.getpid()} started_at={utc_now()}\n")
        self._stream.flush()
        return self

    def __exit__(self, *args: Any) -> None:
        if self._stream is not None:
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
            self._stream.close()
            self._stream = None


@dataclass
class ProductionDependencies:
    channel_manager: Any
    discovery: Any
    downloader: Any | None = None
    manifest: Any | None = None
    transcriber: Any | None = None
    analyzer: Any | None = None
    renderer: Any | None = None
    review_queue: Any | None = None
    known_processed_ids: frozenset[str] = frozenset()


class ProductionRunner:
    def __init__(
        self, dependencies: ProductionDependencies, *, state: ProductionState,
        lock_factory: Callable[[], AbstractContextManager[Any]],
        logger: ProductionLogger, max_videos: int = 3, top: int = 3,
        dry_run: bool = False,
    ) -> None:
        self.dependencies = dependencies
        self.state = state
        self.lock_factory = lock_factory
        self.logger = logger
        self.max_videos = max_videos
        self.top = top
        self.dry_run = dry_run

    def run(self) -> ProductionSummary:
        summary = ProductionSummary(dry_run=self.dry_run)
        with self.lock_factory():
            document = self.state.load()
            if not self.dry_run:
                recovered = self.state.recover_interrupted(document)
                if recovered:
                    self.state.write(document)
                    self.logger.emit("interrupted_recovered", count=recovered)
            channels = self.dependencies.channel_manager.get_enabled_channels()
            processed = self._processed_video_ids(document)
            self.logger.emit(
                "run_started", dry_run=self.dry_run, enabled_creators=len(channels)
            )
            for channel in channels:
                summary.creators_checked += 1
                self._run_creator(channel, document, processed, summary)
            self.logger.emit("run_summary", **asdict(summary))
        return summary

    def _processed_video_ids(self, document: dict[str, Any]) -> set[str]:
        result = {
            video_id for video_id, entry in document["videos"].items()
            if entry["status"] in FINAL_STATES
        }
        queue = self.dependencies.review_queue
        if queue is not None:
            result.update(
                item["video_id"] for item in queue.list_items()
                if item["video_id"] not in document["videos"]
            )
        result.update(
            video_id for video_id in self.dependencies.known_processed_ids
            if video_id not in document["videos"]
        )
        return result

    def _run_creator(
        self, channel: dict[str, Any], document: dict[str, Any],
        processed: set[str], summary: ProductionSummary,
    ) -> None:
        name, url = channel["name"], channel["youtube_url"]
        try:
            maximum = channel_video_limit(channel, self.max_videos)
            discovery = self.dependencies.discovery.discover_recent_channel_metadata(
                name, url, maximum
            )
            if discovery.status is DownloadStatus.FAILED:
                raise ProductionRunnerError(discovery.message)
        except Exception as error:
            summary.failures += 1
            self.logger.emit("creator_failed", creator=name, error=str(error))
            return
        entries = discovery.entries[:maximum]
        self.logger.emit(
            "creator_checked", creator=name, discovered=len(discovery.entries),
            considered=len(entries), max_videos_per_cycle=maximum,
        )
        for metadata in entries:
            video_id = metadata.get("id")
            if not isinstance(video_id, str) or not video_id.strip():
                summary.failures += 1
                self.logger.emit(
                    "video_failed", creator=name,
                    error="Discovered metadata has no video ID.",
                )
                continue
            video_id = video_id.strip()
            if video_id in processed:
                summary.videos_skipped += 1
                self.logger.emit(
                    "video_skipped", creator=name, video_id=video_id,
                    reason="already_processed",
                )
                continue
            summary.new_videos_discovered += 1
            if self.dry_run:
                self.logger.emit(
                    "video_planned", creator=name, video_id=video_id,
                    title=metadata.get("title"),
                )
                continue
            try:
                self._process_video(name, url, metadata, document, summary)
                processed.add(video_id)
            except Exception as error:
                summary.failures += 1
                self.state.update(
                    document, video_id, stage="failed", status="failed",
                    error=str(error),
                )
                self.logger.emit(
                    "video_failed", creator=name, video_id=video_id,
                    error=str(error),
                )

    def _safe_download_exists(self, stored_path: str) -> bool:
        resolver = getattr(self.dependencies.manifest, "paths", None)
        if resolver is None:
            return Path(stored_path).is_file()
        try:
            resolver.resolve(
                stored_path, must_exist=True, regular=True
            )
        except (OSError, ValueError):
            return False
        return True

    def _process_video(
        self, creator: str, channel_url: str, metadata: dict[str, Any],
        document: dict[str, Any], summary: ProductionSummary,
    ) -> None:
        video_id = str(metadata["id"]).strip()
        self.state.begin(document, video_id, creator)
        self.logger.emit("video_started", creator=creator, video_id=video_id)
        manifest = self.dependencies.manifest
        record = manifest.get(video_id)
        reusable_download = bool(
            record and record["status"] == VideoStatus.DOWNLOADED.value
            and isinstance(record.get("local_file_path"), str)
            and self._safe_download_exists(record["local_file_path"])
        )
        if not reusable_download:
            result = self.dependencies.downloader.download_discovered_entry(
                metadata, creator, channel_url
            )
            if result.status is not DownloadStatus.SUCCESS:
                raise ProductionRunnerError(result.message)
            record = manifest.get(video_id)
            if record is None or record["status"] != VideoStatus.DOWNLOADED.value:
                raise ProductionRunnerError("Download completed without a downloaded manifest record.")
        self.state.update(document, video_id, stage="transcription")
        transcription = self.dependencies.transcriber.transcribe(
            video_id=video_id, retry_failed=True
        )
        transcribed = next(
            (item for item in transcription.results if item.video_id == video_id), None
        )
        if transcribed is None or transcribed.status is TranscriptionResultStatus.FAILED:
            raise ProductionRunnerError(
                transcribed.message if transcribed else "Transcription produced no result."
            )
        self.state.update(document, video_id, stage="candidate_analysis")
        analysis = self.dependencies.analyzer.analyze(video_id=video_id)
        analyzed = next(
            (item for item in analysis.results if item.video_id == video_id), None
        )
        if analyzed is None or analyzed.status is AnalysisResultStatus.FAILED:
            raise ProductionRunnerError(
                analyzed.message if analyzed else "Candidate analysis produced no result."
            )
        self.state.update(document, video_id, stage="preview_rendering")
        previews = self.dependencies.renderer.render(
            video_id, top=self.top,
            context_configuration=ContextExpansionConfiguration.for_profile("reaction"),
        )
        if previews.failed:
            messages = "; ".join(
                item.message for item in previews.items if item.status == "failed"
            )
            raise ProductionRunnerError(
                f"{previews.failed} preview(s) failed: {messages}"
            )
        created = previews.successful
        summary.previews_created += created
        summary.videos_processed += 1
        self.state.update(
            document, video_id, stage="review_ready", status="completed",
            preview_count=previews.successful + previews.skipped,
        )
        self.logger.emit(
            "video_completed", creator=creator, video_id=video_id,
            previews_created=created, review_items=previews.successful + previews.skipped,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one local ingestion-to-review production cycle."
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-videos", type=positive_integer, default=3)
    parser.add_argument("--top", type=positive_integer, default=3)
    parser.add_argument("--channel-config", type=Path, default=CONFIG_FILE)
    parser.add_argument("--manifest-path", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--state-path", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--lock-path", type=Path, default=DEFAULT_LOCK_PATH)
    parser.add_argument("--log-path", type=Path, default=DEFAULT_LOG_PATH)
    parser.add_argument("--download-directory", type=Path, default=DOWNLOAD_DIRECTORY)
    parser.add_argument("--archive-directory", type=Path, default=ARCHIVE_DIRECTORY)
    parser.add_argument("--transcript-directory", type=Path, default=DEFAULT_TRANSCRIPT_DIRECTORY)
    parser.add_argument("--candidate-directory", type=Path, default=DEFAULT_CANDIDATE_DIRECTORY)
    parser.add_argument("--preview-directory", type=Path, default=DEFAULT_PREVIEW_DIRECTORY)
    parser.add_argument("--review-queue-path", type=Path, default=DEFAULT_REVIEW_QUEUE_PATH)
    parser.add_argument("--model", default="base.en")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--compute-type", default="int8")
    parser.add_argument("--ffmpeg-path", default="ffmpeg")
    parser.add_argument("--ffprobe-path", default="ffprobe")
    return parser


def create_dependencies(args: argparse.Namespace) -> ProductionDependencies:
    manager = ChannelManager(args.channel_config)
    if args.dry_run:
        discovery = YouTubeDownloader(discovery_only=True)
        return ProductionDependencies(
            manager, discovery,
            known_processed_ids=_read_review_video_ids(args.review_queue_path),
        )
    manifest = VideoManifest(args.manifest_path)
    downloader = YouTubeDownloader(
        args.download_directory, args.archive_directory,
        manifest_path=args.manifest_path, manifest=manifest,
    )
    transcriber = VideoTranscriber(
        output_directory=args.transcript_directory, manifest=manifest,
        model_name=args.model, device=args.device, compute_type=args.compute_type,
    )
    analyzer = ClipCandidateGenerator(
        output_directory=args.candidate_directory, manifest=manifest,
    )
    preview_renderer = VideoPreviewRenderer(
        manifest_path=args.manifest_path, output_directory=args.preview_directory,
        configuration=RenderConfiguration(),
        caption_configuration=CaptionConfiguration(),
        ffmpeg_path=args.ffmpeg_path, ffprobe_path=args.ffprobe_path,
    )
    queue = ClipReviewQueue(args.review_queue_path)
    renderer = BatchPreviewRenderer(preview_renderer, queue)
    return ProductionDependencies(
        manager, downloader, downloader, manifest, transcriber, analyzer,
        renderer, queue,
    )


def _read_review_video_ids(path: Path) -> frozenset[str]:
    """Read only enough existing queue data for dry-run deduplication."""

    path = Path(path)
    if not path.exists():
        return frozenset()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ProductionRunnerError(
            f"Review queue {path} is corrupt: {error}."
        ) from error
    except OSError as error:
        raise ProductionRunnerError(f"Cannot read review queue {path}: {error}.") from error
    items = document.get("items") if isinstance(document, dict) else None
    if not isinstance(items, list):
        raise ProductionRunnerError(f"Review queue {path} has no valid items list.")
    result = set()
    for index, item in enumerate(items):
        video_id = item.get("video_id") if isinstance(item, dict) else None
        if not isinstance(video_id, str) or not video_id:
            raise ProductionRunnerError(
                f"Review queue {path} item {index} has no valid video_id."
            )
        result.add(video_id)
    return frozenset(result)


def main(argv: Sequence[str] | None = ()) -> int:
    args = build_parser().parse_args(argv)
    logger = ProductionLogger(args.log_path, dry_run=args.dry_run)
    try:
        runner = ProductionRunner(
            create_dependencies(args), state=ProductionState(args.state_path),
            lock_factory=lambda: ProductionLock(args.lock_path),
            logger=logger, max_videos=args.max_videos, top=args.top,
            dry_run=args.dry_run,
        )
        summary = runner.run()
    except (OSError, ValueError, ProductionRunnerError) as error:
        logger.emit("run_failed", error=str(error), dry_run=args.dry_run)
        return 1
    return 1 if summary.failures else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(None))
