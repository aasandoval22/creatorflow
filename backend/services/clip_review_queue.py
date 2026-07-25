"""Durable, entirely local review state for rendered clip previews."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

from backend.services.video_manifest import utc_now


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REVIEW_QUEUE_PATH = PROJECT_ROOT / "data" / "review_queue" / "reviews.json"
REVIEW_QUEUE_VERSION = 3
REVIEW_STATUSES = frozenset({"pending", "approved", "rejected"})
ITEM_FIELDS = {
    "review_id", "video_id", "candidate_id", "candidate_rank",
    "candidate_score", "candidate_start", "candidate_end",
    "candidate_duration", "candidate_text", "preview_path",
    "preview_metadata_path", "status", "reviewed_at", "review_note",
    "created_at", "updated_at",
    "render_start", "render_end", "render_duration", "lead_in_seconds",
    "tail_seconds", "timing_revision", "timing_updated_at",
    "timing_source", "context_profile", "context_reasons",
}


class ReviewQueueError(ValueError):
    """Raised when local review state is invalid or cannot be safely changed."""


def _timestamp(value: Any, label: str, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str) or not value:
        raise ReviewQueueError(f"{label} must be a UTC ISO 8601 string.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ReviewQueueError(f"{label} must be a UTC ISO 8601 string.") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ReviewQueueError(f"{label} must include the UTC timezone.")


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReviewQueueError(f"{label} must be numeric.")
    result = float(value)
    if not math.isfinite(result):
        raise ReviewQueueError(f"{label} must be finite.")
    return result


class ClipReviewQueue:
    """Read and atomically update a strictly validated review queue."""

    _registry_guard = threading.Lock()
    _process_locks: dict[Path, threading.RLock] = {}
    _local = threading.local()

    def __init__(
        self, path: Path = DEFAULT_REVIEW_QUEUE_PATH, *,
        process_lock: threading.RLock | None = None,
    ) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_name(f"{self.path.name}.lock")
        if process_lock is None:
            key = self.path.resolve()
            with self._registry_guard:
                process_lock = self._process_locks.setdefault(key, threading.RLock())
        self._process_lock = process_lock
        with self.locked():
            if self.path.exists():
                document = self._read_document()
                if document.get("version") in (1, 2):
                    document = self._migrate_legacy(document)
                    self._write_document(document)
                self._validate_document(document)

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Serialize a complete queue transaction in-process and across processes."""
        with self._process_lock:
            key = str(self.path.resolve())
            depths = getattr(self._local, "depths", {})
            depth = depths.get(key, 0)
            lock_stream = None
            try:
                if depth == 0 and fcntl is not None:
                    self.lock_path.parent.mkdir(parents=True, exist_ok=True)
                    lock_stream = self.lock_path.open("a+b")
                    fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX)
                depths[key] = depth + 1
                self._local.depths = depths
                yield
            finally:
                if depth == 0:
                    depths.pop(key, None)
                    if lock_stream is not None:
                        try:
                            fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)
                        finally:
                            lock_stream.close()
                else:
                    depths[key] = depth

    def _migrate_legacy(self, document: dict[str, Any]) -> dict[str, Any]:
        if set(document) != {"version", "updated_at", "items"} or not isinstance(
            document.get("items"), list
        ):
            raise ReviewQueueError("Legacy review queue has an invalid structure.")
        migrated = copy.deepcopy(document)
        source_version = migrated["version"]
        migrated["version"] = REVIEW_QUEUE_VERSION
        for item in migrated["items"]:
            if not isinstance(item, dict):
                raise ReviewQueueError("Version 1 review queue contains an invalid item.")
            if source_version == 1:
                item.update(
                    render_start=item.get("candidate_start"),
                    render_end=item.get("candidate_end"),
                    render_duration=item.get("candidate_duration"),
                    lead_in_seconds=0.0, tail_seconds=0.0,
                    timing_revision=0, timing_updated_at=None,
                )
            item.update(
                timing_source="manual" if item.get("timing_revision", 0) else "candidate",
                context_profile=None, context_reasons=[],
            )
        self._validate_document(migrated)
        return migrated

    @staticmethod
    def stable_review_id(video_id: str, candidate_id: str) -> str:
        identity = f"{video_id}\0{candidate_id}".encode()
        return f"review_{hashlib.sha256(identity).hexdigest()[:20]}"

    @staticmethod
    def _empty_document() -> dict[str, Any]:
        return {"version": REVIEW_QUEUE_VERSION, "updated_at": utc_now(), "items": []}

    def _read_document(self) -> dict[str, Any]:
        try:
            with self.path.open(encoding="utf-8") as stream:
                document = json.load(stream)
        except json.JSONDecodeError as error:
            raise ReviewQueueError(
                f"Invalid JSON in review queue {self.path}: {error}. "
                "Repair or remove the corrupt file before retrying."
            ) from error
        if not isinstance(document, dict):
            raise ReviewQueueError(f"Review queue {self.path} must contain an object.")
        return document

    def _load(self) -> dict[str, Any]:
        document = self._read_document() if self.path.exists() else self._empty_document()
        self._validate_document(document)
        return document

    def _validate_document(self, document: Any) -> None:
        if not isinstance(document, dict) or set(document) != {"version", "updated_at", "items"}:
            raise ReviewQueueError("Review queue must contain exactly version, updated_at, and items.")
        if document["version"] != REVIEW_QUEUE_VERSION:
            raise ReviewQueueError(
                f"Review queue version must be {REVIEW_QUEUE_VERSION}; "
                f"found {document['version']!r}."
            )
        _timestamp(document["updated_at"], "Queue updated_at")
        if not isinstance(document["items"], list):
            raise ReviewQueueError("Review queue items must be a list.")
        identities: set[tuple[str, str]] = set()
        review_ids: set[str] = set()
        for index, item in enumerate(document["items"]):
            self._validate_item(item, index)
            identity = (item["video_id"], item["candidate_id"])
            if identity in identities or item["review_id"] in review_ids:
                raise ReviewQueueError(f"Duplicate review identity at item {index}.")
            identities.add(identity)
            review_ids.add(item["review_id"])

    def _validate_item(self, item: Any, index: int = 0) -> None:
        label = f"Review item {index}"
        if not isinstance(item, dict) or set(item) != ITEM_FIELDS:
            raise ReviewQueueError(f"{label} has missing or unknown fields.")
        for field in ("review_id", "video_id", "candidate_id", "candidate_text",
                      "preview_path", "preview_metadata_path"):
            if not isinstance(item[field], str) or not item[field].strip():
                raise ReviewQueueError(f"{label} {field} must be a nonempty string.")
        expected_id = self.stable_review_id(item["video_id"], item["candidate_id"])
        if item["review_id"] != expected_id:
            raise ReviewQueueError(f"{label} review_id is not deterministic for its identity.")
        rank = item["candidate_rank"]
        if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
            raise ReviewQueueError(f"{label} candidate_rank must be a positive integer.")
        score = _number(item["candidate_score"], f"{label} candidate_score")
        if not 0 <= score <= 100:
            raise ReviewQueueError(f"{label} candidate_score must be between 0 and 100.")
        start = _number(item["candidate_start"], f"{label} candidate_start")
        end = _number(item["candidate_end"], f"{label} candidate_end")
        duration = _number(item["candidate_duration"], f"{label} candidate_duration")
        if start < 0 or end <= start or duration <= 0 or abs(duration - (end - start)) > 0.1:
            raise ReviewQueueError(f"{label} candidate timestamps and duration are invalid.")
        render_start = _number(item["render_start"], f"{label} render_start")
        render_end = _number(item["render_end"], f"{label} render_end")
        render_duration = _number(item["render_duration"], f"{label} render_duration")
        lead = _number(item["lead_in_seconds"], f"{label} lead_in_seconds")
        tail = _number(item["tail_seconds"], f"{label} tail_seconds")
        if lead < 0 or tail < 0:
            raise ReviewQueueError(f"{label} lead-in and tail must be nonnegative.")
        if (
            render_start < 0 or render_end <= render_start
            or render_start > start or render_end < end
            or abs(render_duration - (render_end - render_start)) > 0.1
        ):
            raise ReviewQueueError(f"{label} render timestamps and duration are invalid.")
        if abs(lead - (start - render_start)) > 0.1 or abs(tail - (render_end - end)) > 0.1:
            raise ReviewQueueError(f"{label} lead-in or tail disagrees with the render range.")
        revision = item["timing_revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ReviewQueueError(f"{label} timing_revision must be a nonnegative integer.")
        _timestamp(item["timing_updated_at"], f"{label} timing_updated_at", nullable=True)
        if item["timing_source"] not in {"candidate", "automatic", "manual"}:
            raise ReviewQueueError(f"{label} timing_source is invalid.")
        if item["context_profile"] is not None and (
            not isinstance(item["context_profile"], str) or not item["context_profile"].strip()
        ):
            raise ReviewQueueError(f"{label} context_profile must be a string or null.")
        if not isinstance(item["context_reasons"], list) or any(
            not isinstance(reason, str) or not reason.strip() for reason in item["context_reasons"]
        ):
            raise ReviewQueueError(f"{label} context_reasons must be a list of strings.")
        status = item["status"]
        if status not in REVIEW_STATUSES:
            raise ReviewQueueError(f"{label} status must be pending, approved, or rejected.")
        if status == "pending" and item["reviewed_at"] is not None:
            raise ReviewQueueError(f"{label} pending reviewed_at must be null.")
        if status != "pending" and item["reviewed_at"] is None:
            raise ReviewQueueError(f"{label} reviewed items require reviewed_at.")
        _timestamp(item["reviewed_at"], f"{label} reviewed_at", nullable=True)
        _timestamp(item["created_at"], f"{label} created_at")
        _timestamp(item["updated_at"], f"{label} updated_at")
        if item["review_note"] is not None and not isinstance(item["review_note"], str):
            raise ReviewQueueError(f"{label} review_note must be a string or null.")

    def _write_document(self, document: dict[str, Any]) -> None:
        self._validate_document(document)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.path.parent,
                prefix=".reviews.", suffix=".tmp", delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(document, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def list_items(
        self, *, status: str | None = None, video_id: str | None = None
    ) -> list[dict[str, Any]]:
        if status is not None and status not in REVIEW_STATUSES:
            raise ReviewQueueError(f"Unknown review status {status!r}.")
        with self.locked():
            items = self._load()["items"]
        selected = [
            item for item in items
            if (status is None or item["status"] == status)
            and (video_id is None or item["video_id"] == video_id)
        ]
        return copy.deepcopy(selected)

    def find_by_review_id(self, review_id: str) -> dict[str, Any] | None:
        return next((item for item in self.list_items() if item["review_id"] == review_id), None)

    def find_by_candidate(self, video_id: str, candidate_id: str) -> dict[str, Any] | None:
        return next((
            item for item in self.list_items(video_id=video_id)
            if item["candidate_id"] == candidate_id
        ), None)

    def add_or_update_preview(
        self, video_id: str, candidate: dict[str, Any],
        preview_path: str | Path, preview_metadata_path: str | Path,
        *, render_start: float | None = None, render_end: float | None = None,
        timing_source: str = "candidate", context_profile: str | None = None,
        context_reasons: list[str] | tuple[str, ...] = (),
    ) -> dict[str, Any]:
        with self.locked():
            return self._add_or_update_preview(
                video_id, candidate, preview_path, preview_metadata_path,
                render_start=render_start, render_end=render_end,
                timing_source=timing_source, context_profile=context_profile,
                context_reasons=context_reasons,
            )

    def _add_or_update_preview(
        self, video_id: str, candidate: dict[str, Any],
        preview_path: str | Path, preview_metadata_path: str | Path,
        *, render_start: float | None = None, render_end: float | None = None,
        timing_source: str = "candidate", context_profile: str | None = None,
        context_reasons: list[str] | tuple[str, ...] = (),
    ) -> dict[str, Any]:
        document = self._load()
        now = utc_now()
        candidate_id = candidate.get("candidate_id")
        if not isinstance(video_id, str) or not video_id.strip():
            raise ReviewQueueError("Video ID must be a nonempty string.")
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            raise ReviewQueueError("Candidate ID must be a nonempty string.")
        identity = (video_id, candidate_id)
        existing = next((
            item for item in document["items"]
            if (item["video_id"], item["candidate_id"]) == identity
        ), None)
        preserved = existing or {}
        preserve_candidate = bool(existing and existing.get("timing_source") == "manual")
        preserve_timing = bool(existing and existing.get("timing_source") == "manual")
        chosen_start = preserved.get("render_start") if preserve_timing else (
            candidate.get("start") if render_start is None else render_start
        )
        chosen_end = preserved.get("render_end") if preserve_timing else (
            candidate.get("end") if render_end is None else render_end
        )
        candidate_start = preserved["candidate_start"] if preserve_candidate else candidate.get("start")
        candidate_end = preserved["candidate_end"] if preserve_candidate else candidate.get("end")
        item = {
            "review_id": self.stable_review_id(*identity),
            "video_id": video_id,
            "candidate_id": candidate_id,
            "candidate_rank": preserved["candidate_rank"] if preserve_candidate else candidate.get("rank"),
            "candidate_score": preserved["candidate_score"] if preserve_candidate else candidate.get("score"),
            "candidate_start": candidate_start,
            "candidate_end": candidate_end,
            "candidate_duration": preserved["candidate_duration"] if preserve_candidate else candidate.get("duration"),
            "candidate_text": preserved["candidate_text"] if preserve_candidate else candidate.get("text"),
            "render_start": chosen_start,
            "render_end": chosen_end,
            "render_duration": chosen_end - chosen_start,
            "lead_in_seconds": candidate_start - chosen_start,
            "tail_seconds": chosen_end - candidate_end,
            "timing_revision": preserved.get("timing_revision", 0),
            "timing_updated_at": preserved.get("timing_updated_at"),
            "timing_source": preserved.get("timing_source") if preserve_timing else timing_source,
            "context_profile": preserved.get("context_profile") if preserve_timing else context_profile,
            "context_reasons": preserved.get("context_reasons") if preserve_timing else list(context_reasons),
            "preview_path": str(preview_path),
            "preview_metadata_path": str(preview_metadata_path),
            "status": preserved.get("status", "pending"),
            "reviewed_at": preserved.get("reviewed_at"),
            "review_note": preserved.get("review_note"),
            "created_at": preserved.get("created_at", now),
            "updated_at": now,
        }
        self._validate_item(item)
        if existing is None:
            document["items"].append(item)
        else:
            document["items"][document["items"].index(existing)] = item
        document["updated_at"] = now
        self._write_document(document)
        return copy.deepcopy(item)

    def update_timing(
        self, review_id: str, *, render_start: float, render_end: float,
        preview_path: str | Path, preview_metadata_path: str | Path,
        note: str | None = None, clear_note: bool = False,
        timing_source: str = "manual", context_profile: str | None = None,
        context_reasons: list[str] | tuple[str, ...] = (),
    ) -> dict[str, Any]:
        with self.locked():
            return self._update_timing(
                review_id, render_start=render_start, render_end=render_end,
                preview_path=preview_path, preview_metadata_path=preview_metadata_path,
                note=note, clear_note=clear_note,
                timing_source=timing_source, context_profile=context_profile,
                context_reasons=context_reasons,
            )

    def _update_timing(
        self, review_id: str, *, render_start: float, render_end: float,
        preview_path: str | Path, preview_metadata_path: str | Path,
        note: str | None = None, clear_note: bool = False,
        timing_source: str = "manual", context_profile: str | None = None,
        context_reasons: list[str] | tuple[str, ...] = (),
    ) -> dict[str, Any]:
        if note is not None and clear_note:
            raise ReviewQueueError("A note and clear_note cannot be used together.")
        document = self._load()
        item = next((value for value in document["items"] if value["review_id"] == review_id), None)
        if item is None:
            raise ReviewQueueError(f"Review ID {review_id!r} was not found.")
        now = utc_now()
        start = round(float(render_start), 3)
        end = round(float(render_end), 3)
        item.update(
            render_start=start, render_end=end,
            render_duration=round(end - start, 3),
            lead_in_seconds=round(item["candidate_start"] - start, 3),
            tail_seconds=round(end - item["candidate_end"], 3),
            timing_revision=item["timing_revision"] + 1,
            timing_updated_at=now,
            timing_source=timing_source, context_profile=context_profile,
            context_reasons=list(context_reasons),
            preview_path=str(preview_path),
            preview_metadata_path=str(preview_metadata_path),
            status="pending", reviewed_at=None, updated_at=now,
        )
        if clear_note:
            item["review_note"] = None
        elif note is not None:
            item["review_note"] = note
        document["updated_at"] = now
        self._write_document(document)
        return copy.deepcopy(item)

    def _change(
        self, review_id: str, *, status: str | None = None,
        note: str | None = None, change_note: bool = False,
    ) -> dict[str, Any]:
        with self.locked():
            document = self._load()
            item = next((value for value in document["items"] if value["review_id"] == review_id), None)
            if item is None:
                raise ReviewQueueError(f"Review ID {review_id!r} was not found.")
            now = utc_now()
            if status is not None:
                if status not in REVIEW_STATUSES:
                    raise ReviewQueueError(f"Unknown review status {status!r}.")
                item["status"] = status
                item["reviewed_at"] = None if status == "pending" else now
            if change_note:
                if note is not None and not isinstance(note, str):
                    raise ReviewQueueError("Review note must be a string or null.")
                item["review_note"] = note
            item["updated_at"] = now
            document["updated_at"] = now
            self._write_document(document)
            return copy.deepcopy(item)

    def approve(self, review_id: str, note: str | None = None) -> dict[str, Any]:
        return self._change(review_id, status="approved", note=note, change_note=note is not None)

    def reject(self, review_id: str, note: str | None = None) -> dict[str, Any]:
        return self._change(review_id, status="rejected", note=note, change_note=note is not None)

    def return_to_pending(
        self, review_id: str, note: str | None = None, *, clear_note: bool = False
    ) -> dict[str, Any]:
        if clear_note and note is not None:
            raise ReviewQueueError("A note and clear_note cannot be used together.")
        return self._change(
            review_id, status="pending", note=None if clear_note else note,
            change_note=clear_note or note is not None,
        )

    def update_note(self, review_id: str, note: str | None) -> dict[str, Any]:
        return self._change(review_id, note=note, change_note=True)
