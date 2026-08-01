"""Immutable, profile-pinned snapshots for read-only review comparisons."""

from __future__ import annotations

import fcntl
import hashlib
import json
import re
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from backend.services.clip_review_queue import ClipReviewQueue
from backend.services.reference_clip_comparator import ReferenceClipComparator
from backend.services.reference_clip_library import PROJECT_ROOT, _atomic_json
from backend.services.reference_evidence_service import _atomic_restore
from backend.services.reference_profile_builder import ReferenceProfileBuilder
from backend.services.video_manifest import utc_now


DEFAULT_BATCH_ROOT = PROJECT_ROOT / "data" / "review_comparison_batches"
BATCH_ID = re.compile(r"batch_[0-9a-f]{24}")


class ReviewComparisonBatchError(ValueError):
    """A stable comparison batch is malformed or cannot be persisted."""


class ReviewComparisonBatchService:
    def __init__(
        self, queue: ClipReviewQueue, profile_builder: ReferenceProfileBuilder,
        comparator: ReferenceClipComparator, *, root: Path = DEFAULT_BATCH_ROOT,
    ) -> None:
        self.queue = queue
        self.profile_builder = profile_builder
        self.comparator = comparator
        self.root = Path(root)

    def capture(self, profile_name: str) -> dict[str, Any]:
        profile = self.profile_builder.read(profile_name)
        path = self.profile_builder.profile_path(profile_name)
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        with self.queue.locked():
            items = self.queue._load()["items"]
            pending = [dict(item) for item in items if item["status"] == "pending"]
        batch_id = f"batch_{uuid.uuid4().hex[:24]}"
        directory = self.path(batch_id)
        directory.mkdir(parents=True, mode=0o700)
        captured_at = utc_now()
        manifest = {
            "version": 1,
            "batch_id": batch_id,
            "captured_at": captured_at,
            "profile_name": profile_name,
            "profile_sha256": digest,
            "profile_version": profile["version"],
            "profile_built_at": profile.get("built_at"),
            "items": [
                {
                    "review_id": item["review_id"],
                    "captured_status": item["status"],
                    "captured_updated_at": item["updated_at"],
                    "captured_timing_revision": item["timing_revision"],
                    "review_snapshot": item,
                }
                for item in pending
            ],
        }
        try:
            _atomic_restore(directory / "profile.json", raw)
            _atomic_json(directory / "manifest.json", manifest)
        except Exception:
            # The unique incomplete directory contains no live state and is not
            # considered a batch until its strict manifest exists.
            raise
        return manifest

    def run(self, batch_id: str) -> dict[str, Any]:
        with self._locked(batch_id):
            manifest, profile = self._load(batch_id)
            summary_path = self.path(batch_id) / "run.json"
            if summary_path.is_file():
                summary = self._read_json(summary_path)
                if summary.get("status") == "completed":
                    return summary
            current = {item["review_id"]: item for item in self.queue.list_items()}
            reports = []
            reports_directory = self.path(batch_id) / "reports"
            for captured in manifest["items"]:
                review_id = captured["review_id"]
                now = current.get(review_id)
                current_status = now["status"] if now else "missing"
                changed = (
                    now is None
                    or now["status"] != captured["captured_status"]
                    or now["updated_at"] != captured["captured_updated_at"]
                    or now["timing_revision"] != captured["captured_timing_revision"]
                )
                report = self.comparator.compare(
                    manifest["profile_name"], captured["review_snapshot"],
                    profile_document=profile, created_at=manifest["captured_at"],
                    write=False,
                )
                report["batch"] = {
                    "batch_id": batch_id,
                    "captured_at": manifest["captured_at"],
                    "profile_sha256": manifest["profile_sha256"],
                    "profile_version": manifest["profile_version"],
                    "profile_built_at": manifest["profile_built_at"],
                    "captured_status": captured["captured_status"],
                    "current_status": current_status,
                    "status_or_revision_changed": changed,
                }
                _atomic_json(reports_directory / f"{review_id}.json", report)
                reports.append({
                    "review_id": review_id,
                    "current_status": current_status,
                    "status_or_revision_changed": changed,
                    "report_path": str(reports_directory / f"{review_id}.json"),
                })
            summary = {
                "version": 1, "batch_id": batch_id, "status": "completed",
                "completed_at": utc_now(), "profile_sha256": manifest["profile_sha256"],
                "item_count": len(reports), "reports": reports,
            }
            _atomic_json(summary_path, summary)
            return summary

    def show(self, batch_id: str) -> dict[str, Any]:
        manifest, _ = self._load(batch_id)
        run_path = self.path(batch_id) / "run.json"
        return {"manifest": manifest, "run": self._read_json(run_path) if run_path.is_file() else None}

    def latest_reports(self, review_ids: set[str]) -> dict[str, dict[str, Any]]:
        result: dict[str, tuple[str, dict[str, Any]]] = {}
        if not self.root.is_dir():
            return {}
        for directory in sorted(self.root.glob("batch_*")):
            try:
                shown = self.show(directory.name)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if not shown["run"] or shown["run"].get("status") != "completed":
                continue
            captured_at = shown["manifest"]["captured_at"]
            for item in shown["manifest"]["items"]:
                review_id = item["review_id"]
                if review_id not in review_ids:
                    continue
                report_path = directory / "reports" / f"{review_id}.json"
                try:
                    report = self._read_json(report_path)
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                if review_id not in result or captured_at > result[review_id][0]:
                    result[review_id] = (captured_at, report)
        return {key: value[1] for key, value in result.items()}

    def path(self, batch_id: str) -> Path:
        if not isinstance(batch_id, str) or not BATCH_ID.fullmatch(batch_id):
            raise ReviewComparisonBatchError("Comparison batch ID is invalid.")
        return self.root / batch_id

    def _load(self, batch_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        directory = self.path(batch_id)
        manifest = self._read_json(directory / "manifest.json")
        required = {
            "version", "batch_id", "captured_at", "profile_name", "profile_sha256",
            "profile_version", "profile_built_at", "items",
        }
        if set(manifest) != required or manifest.get("version") != 1 or manifest.get("batch_id") != batch_id:
            raise ReviewComparisonBatchError("Comparison batch manifest is malformed.")
        raw = (directory / "profile.json").read_bytes()
        if hashlib.sha256(raw).hexdigest() != manifest["profile_sha256"]:
            raise ReviewComparisonBatchError("Pinned comparison profile checksum changed.")
        try:
            profile = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ReviewComparisonBatchError("Pinned comparison profile is malformed.") from error
        if profile.get("profile_name") != manifest["profile_name"] or profile.get("version") != manifest["profile_version"]:
            raise ReviewComparisonBatchError("Pinned comparison profile identity changed.")
        seen: set[str] = set()
        for item in manifest["items"]:
            if not isinstance(item, dict) or set(item) != {
                "review_id", "captured_status", "captured_updated_at",
                "captured_timing_revision", "review_snapshot",
            } or item["review_id"] in seen or item["review_snapshot"].get("review_id") != item["review_id"]:
                raise ReviewComparisonBatchError("Comparison batch item is malformed.")
            seen.add(item["review_id"])
        return manifest, profile

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ReviewComparisonBatchError(f"{path} must contain a JSON object.")
        return value

    @contextmanager
    def _locked(self, batch_id: str) -> Iterator[None]:
        lock = self.path(batch_id) / ".run.lock"
        with lock.open("a+") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
