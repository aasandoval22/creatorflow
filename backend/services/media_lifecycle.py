"""Ownership-aware media retention planning and two-stage local cleanup."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from backend.services.clip_review_queue import ClipReviewQueue
from backend.services.production_runner import ProductionLock
from backend.services.publication import (
    PublicationState,
    PublicationStore,
    safe_publication_text,
    sha256_file,
)
from backend.services.reference_clip_library import ReferenceClipLibrary
from backend.services.video_manifest import VideoManifest, utc_now


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_ROOT = PROJECT_ROOT / "data"
DEFAULT_POLICY_PATH = PROJECT_ROOT / "backend" / "config" / "media_retention.json"
DEFAULT_CLEANUP_ROOT = DEFAULT_DATA_ROOT / "media_cleanup"
DEFAULT_PLAN_ROOT = DEFAULT_CLEANUP_ROOT / "plans"
DEFAULT_QUARANTINE_ROOT = DEFAULT_CLEANUP_ROOT / "quarantine"
DEFAULT_CLEANUP_AUDIT = DEFAULT_CLEANUP_ROOT / "events.jsonl"
PLAN_ID = re.compile(r"plan_[0-9a-f]{24}")
QUARANTINE_ID = re.compile(r"quarantine_[0-9a-f]{24}")
SHA256 = re.compile(r"[0-9a-f]{64}")


class MediaLifecycleError(ValueError):
    """Media ownership or cleanup state is invalid or unsafe."""


@dataclass(frozen=True)
class RetentionPolicy:
    source_media_retention_days: int = 7
    rejected_preview_retention_days: int = 14
    published_media_retention_days: int = 30
    quarantine_retention_days: int = 7
    retain_metadata: bool = True
    retain_transcripts: bool = True
    retain_audits: bool = True

    @classmethod
    def read(cls, path: Path = DEFAULT_POLICY_PATH) -> "RetentionPolicy":
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise MediaLifecycleError(f"Retention policy {path} is corrupt.") from error
        if not isinstance(value, dict) or set(value) != set(asdict(cls())):
            raise MediaLifecycleError("Retention policy has missing or unknown fields.")
        policy = cls(**value)
        policy.validate()
        return policy

    def validate(self) -> None:
        for name in (
            "source_media_retention_days", "rejected_preview_retention_days",
            "published_media_retention_days", "quarantine_retention_days",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise MediaLifecycleError(f"Retention policy {name} must be nonnegative.")
        for name in ("retain_metadata", "retain_transcripts", "retain_audits"):
            if not isinstance(getattr(self, name), bool):
                raise MediaLifecycleError(f"Retention policy {name} must be boolean.")


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise MediaLifecycleError(f"Invalid lifecycle timestamp {value!r}.") from error
    if parsed.tzinfo is None:
        raise MediaLifecycleError(f"Lifecycle timestamp {value!r} lacks timezone.")
    return parsed.astimezone(timezone.utc)


def _atomic_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(document, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _content_hash(document: Mapping[str, Any]) -> str:
    value = {key: copy.deepcopy(value) for key, value in document.items() if key != "content_sha256"}
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class MediaOwnershipGraph:
    """Join authoritative metadata to every reproducible media dependency."""

    def __init__(
        self, *, data_root: Path = DEFAULT_DATA_ROOT,
        manifest: VideoManifest | None = None,
        queue: ClipReviewQueue | None = None,
        publications: PublicationStore | None = None,
        references: ReferenceClipLibrary | None = None,
    ) -> None:
        self.data_root = Path(data_root).resolve()
        self.manifest = manifest or VideoManifest(
            self.data_root / "manifests" / "videos.json"
        )
        self.queue = queue or ClipReviewQueue(
            self.data_root / "review_queue" / "reviews.json"
        )
        self.publications = publications or PublicationStore(
            self.data_root / "publication" / "records.json",
            self.data_root / "publication" / "events.jsonl",
        )
        index = self.data_root / "reference_clips" / "index.json"
        self.references = references or ReferenceClipLibrary(
            self.data_root / "reference_clips", index
        )

    def build(self) -> dict[str, Any]:
        reviews = self.queue.list_items()
        attempts = self.publications.list_attempts()
        references = (
            self.references.list_references() if self.references.index_path.exists() else []
        )
        review_by_video: dict[str, list[dict[str, Any]]] = {}
        for review in reviews:
            review_by_video.setdefault(review["video_id"], []).append(review)
        attempts_by_review: dict[str, list[dict[str, Any]]] = {}
        for attempt in attempts:
            attempts_by_review.setdefault(attempt["review_id"], []).append(attempt)
        sources = []
        for record in self.manifest.read_records():
            source_reviews = []
            for review in review_by_video.get(record["video_id"], []):
                source_reviews.append({
                    "review_id": review["review_id"],
                    "candidate_id": review["candidate_id"],
                    "status": review["status"],
                    "timing_revision": review["timing_revision"],
                    "rendered_preview": review["preview_path"],
                    "preview_metadata": review["preview_metadata_path"],
                    "approved_final_render": (
                        review["preview_path"] if review["status"] == "approved" else None
                    ),
                    "publication_attempts": attempts_by_review.get(
                        review["review_id"], []
                    ),
                })
            transcription = record.get("transcription") or {}
            analysis = record.get("clip_analysis") or {}
            sources.append({
                "source_creator": record.get("channel_name") or record.get("uploader"),
                "source_video_id": record["video_id"],
                "source_manifest": str(self.manifest.path),
                "downloaded_source_media": record.get("local_file_path"),
                "transcript_and_word_timings": transcription.get("transcript_json_path"),
                "transcript_text": transcription.get("transcript_text_path"),
                "subtitle": transcription.get("subtitle_srt_path"),
                "candidate_artifact": analysis.get("candidates_json_path"),
                "reviews": source_reviews,
            })
        return {
            "version": 1,
            "generated_at": utc_now(),
            "data_root": str(self.data_root),
            "ownership": {
                "authoritative": [
                    "source manifest", "review decisions and timing revisions",
                    "publication records and audits", "reference index and annotations",
                ],
                "derived_reproducible": [
                    "downloaded source media", "transcripts", "candidate artifacts",
                    "rendered previews", "comparison reports",
                ],
                "irreplaceable_local": [
                    "human review notes and decisions", "rights confirmations",
                    "publication history", "reference annotations", "recovery manifests",
                ],
                "always_retained": [
                    "metadata", "transcripts", "checksums", "audit ledgers",
                    "reference media", "comparison batches", "recovery directories",
                ],
            },
            "sources": sources,
            "reference_media": [entry["media_path"] for entry in references],
            "comparison_batch_root": str(self.data_root / "review_comparison_batches"),
            "recovery_roots": [
                str(self.data_root / "reference_evidence_recovery"),
                str(self.data_root / "reference_discovery" / "withdrawal_recovery"),
            ],
        }


class MediaCleanupService:
    """Plan, quarantine, restore, and separately purge eligible media."""

    def __init__(
        self, graph: MediaOwnershipGraph, *,
        policy: RetentionPolicy | None = None,
        cleanup_root: Path | None = None,
        now: Callable[[], datetime] | None = None,
        owner_uid: int | None = None,
        production_lock_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.graph = graph
        self.data_root = graph.data_root
        self.policy = policy or RetentionPolicy.read()
        self.cleanup_root = Path(cleanup_root or self.data_root / "media_cleanup")
        self.plan_root = self.cleanup_root / "plans"
        self.quarantine_root = self.cleanup_root / "quarantine"
        self.audit_path = self.cleanup_root / "events.jsonl"
        self.lock_path = self.cleanup_root / "cleanup.lock"
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.owner_uid = os.getuid() if owner_uid is None else owner_uid
        self.production_lock_factory = production_lock_factory or (
            lambda: ProductionLock(self.data_root / "production" / "production.lock")
        )

    def ownership(self) -> dict[str, Any]:
        return self.graph.build()

    def plan(self) -> dict[str, Any]:
        plan_id = f"plan_{uuid.uuid4().hex[:24]}"
        document = {
            "version": 1,
            "plan_id": plan_id,
            "created_at": self.now().isoformat(),
            "data_root": str(self.data_root),
            "policy": asdict(self.policy),
            "items": self.evaluate(),
        }
        document["content_sha256"] = _content_hash(document)
        path = self.plan_path(plan_id)
        if path.exists():
            raise MediaLifecycleError("Cleanup plan identity unexpectedly already exists.")
        _atomic_json(path, document)
        self._audit("plan_created", plan_id=plan_id, item_count=len(document["items"]))
        return document

    def evaluate(self) -> list[dict[str, Any]]:
        now = self.now().astimezone(timezone.utc)
        graph = self.graph.build()
        reference_paths = {
            str(Path(path).expanduser().resolve()) for path in graph["reference_media"]
        }
        active_comparisons = self._active_comparison_reviews()
        recovery_video_ids = self._recovery_video_ids()
        attempts = self.graph.publications.list_attempts()
        attempts_by_review: dict[str, list[dict[str, Any]]] = {}
        for attempt in attempts:
            attempts_by_review.setdefault(attempt["review_id"], []).append(attempt)
        items: list[dict[str, Any]] = []
        records = {item["video_id"]: item for item in self.graph.manifest.read_records()}
        reviews = self.graph.queue.list_items()
        reviews_by_video: dict[str, list[dict[str, Any]]] = {}
        for review in reviews:
            reviews_by_video.setdefault(review["video_id"], []).append(review)

        for video_id, record in records.items():
            raw_path = record.get("local_file_path")
            if not raw_path:
                continue
            path = Path(raw_path).expanduser()
            dependents = reviews_by_video.get(video_id, [])
            reasons: list[str] = []
            eligible_base: list[datetime] = []
            if str(path) in reference_paths:
                reasons.append("source media is a current accepted reference")
            if video_id in recovery_video_ids:
                reasons.append("source media is named by a recovery operation")
            if not dependents:
                reasons.append("source has no terminal review evidence")
            for review in dependents:
                if review["status"] == "pending":
                    reasons.append(f"review {review['review_id']} is pending")
                elif review["status"] == "rejected":
                    reviewed = _parse_time(review["reviewed_at"])
                    if reviewed:
                        eligible_base.append(reviewed)
                else:
                    checksum = self._current_checksum(review)
                    success = self._successful_attempt(
                        attempts_by_review.get(review["review_id"], []),
                        review, checksum,
                    )
                    if success is None:
                        reasons.append(
                            f"approved review {review['review_id']} lacks verified publication"
                        )
                    else:
                        completed = _parse_time(success["publish_completed_at"])
                        if completed:
                            eligible_base.append(completed)
                if review["review_id"] in active_comparisons:
                    reasons.append(
                        f"review {review['review_id']} has an active comparison operation"
                    )
                if self._attempt_blocks(attempts_by_review.get(review["review_id"], [])):
                    reasons.append(
                        f"review {review['review_id']} has unresolved or failed publication state"
                    )
            downloaded = _parse_time(record.get("downloaded_at"))
            if downloaded:
                eligible_base.append(downloaded)
            eligible_at = (
                max(eligible_base) + timedelta(days=self.policy.source_media_retention_days)
                if eligible_base else None
            )
            if eligible_at is None or eligible_at > now:
                reasons.append("source-media retention period has not elapsed")
            items.append(self._item(
                path, kind="source_media", reasons=reasons,
                eligible_at=eligible_at, identity={"video_id": video_id},
            ))

        for review in reviews:
            path = Path(review["preview_path"]).expanduser()
            reasons = []
            eligible_at = None
            review_attempts = attempts_by_review.get(review["review_id"], [])
            try:
                if str(path.resolve()) in reference_paths:
                    reasons.append("preview media is a current accepted reference")
            except OSError:
                pass
            if review["review_id"] in active_comparisons:
                reasons.append("preview has an active comparison operation")
            if review["status"] == "pending":
                reasons.append("pending preview is operationally required")
            elif review["status"] == "rejected":
                rejected_at = _parse_time(review["reviewed_at"])
                eligible_at = (
                    rejected_at + timedelta(
                        days=self.policy.rejected_preview_retention_days
                    ) if rejected_at else None
                )
                if eligible_at is None or eligible_at > now:
                    reasons.append("rejected-preview retention period has not elapsed")
                if self._attempt_blocks(review_attempts):
                    reasons.append("preview has unresolved or failed publication state")
            else:
                checksum = self._current_checksum(review)
                success = self._successful_attempt(review_attempts, review, checksum)
                if success is None:
                    reasons.append("approved but unpublished media is retained")
                else:
                    completed = _parse_time(success["publish_completed_at"])
                    eligible_at = (
                        completed + timedelta(
                            days=self.policy.published_media_retention_days
                        ) if completed else None
                    )
                    if eligible_at is None or eligible_at > now:
                        reasons.append("published-media retention period has not elapsed")
                if self._attempt_blocks(review_attempts):
                    reasons.append("preview has unresolved or failed publication state")
            items.append(self._item(
                path, kind="rendered_preview", reasons=reasons,
                eligible_at=eligible_at,
                identity={
                    "video_id": review["video_id"],
                    "review_id": review["review_id"],
                    "candidate_id": review["candidate_id"],
                    "timing_revision": review["timing_revision"],
                },
            ))
        return sorted(items, key=lambda item: (item["kind"], item["relative_path"]))

    def _item(
        self, path: Path, *, kind: str, reasons: list[str],
        eligible_at: datetime | None, identity: Mapping[str, Any],
    ) -> dict[str, Any]:
        safety_error = None
        size = 0
        checksum = None
        relative = None
        protected_reason = self._protected_path_reason(path)
        if protected_reason:
            reasons.append(protected_reason)
        try:
            safe = self._safe_file(path)
            relative = safe.relative_to(self.data_root).as_posix()
            size = safe.stat().st_size
            checksum = sha256_file(safe)
        except (OSError, MediaLifecycleError) as error:
            safety_error = safe_publication_text(error)
            try:
                relative = path.relative_to(self.data_root).as_posix()
            except ValueError:
                relative = "<outside-root>"
        blockers = sorted(set(reasons + ([safety_error] if safety_error else [])))
        state = {
            "kind": kind,
            "relative_path": relative,
            "size_bytes": size,
            "sha256": checksum,
            "eligible": not blockers,
            "eligible_at": eligible_at.isoformat() if eligible_at else None,
            "reasons": blockers or ["all lifecycle and retention checks passed"],
            "identity": dict(identity),
        }
        state["state_fingerprint"] = hashlib.sha256(
            json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return state

    def _protected_path_reason(self, path: Path) -> str | None:
        try:
            relative = Path(os.path.abspath(path)).relative_to(self.data_root)
        except ValueError:
            return None
        protected = (
            ("reference_clips",),
            ("reference_evidence_recovery",),
            ("reference_discovery", "withdrawal_recovery"),
            ("review_comparison_batches",),
        )
        if any(relative.parts[:len(prefix)] == prefix for prefix in protected):
            return "path belongs to protected reference, comparison, or recovery evidence"
        return None

    @staticmethod
    def _successful_attempt(
        attempts: list[dict[str, Any]], review: Mapping[str, Any],
        checksum: str | None,
    ) -> dict[str, Any] | None:
        return next((
            attempt for attempt in attempts
            if attempt["state"] == PublicationState.PUBLISH_COMPLETE.value
            and not attempt["stale"]
            and attempt["timing_revision"] == review["timing_revision"]
            and checksum is not None
            and attempt["rendered_media_sha256"] == checksum
        ), None)

    @staticmethod
    def _attempt_blocks(attempts: list[dict[str, Any]]) -> bool:
        return any(
            attempt["state"] not in {
                PublicationState.PUBLISH_COMPLETE.value,
                PublicationState.CANCELLED.value,
            }
            for attempt in attempts
        )

    @staticmethod
    def _current_checksum(review: Mapping[str, Any]) -> str | None:
        path = Path(str(review.get("preview_path") or ""))
        return sha256_file(path) if path.is_file() and not path.is_symlink() else None

    def _active_comparison_reviews(self) -> set[str]:
        root = self.data_root / "review_comparison_batches"
        active: set[str] = set()
        if not root.is_dir():
            return active
        for directory in root.glob("batch_*"):
            manifest = directory / "manifest.json"
            run = directory / "run.json"
            try:
                value = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            complete = False
            try:
                complete = json.loads(run.read_text(encoding="utf-8")).get("status") == "completed"
            except (OSError, json.JSONDecodeError):
                pass
            if not complete:
                active.update(
                    item.get("review_id") for item in value.get("items", [])
                    if isinstance(item, dict) and isinstance(item.get("review_id"), str)
                )
        return active

    def _recovery_video_ids(self) -> set[str]:
        result: set[str] = set()
        roots = (
            self.data_root / "reference_evidence_recovery",
            self.data_root / "reference_discovery" / "withdrawal_recovery",
        )
        for root in roots:
            if not root.is_dir():
                continue
            for path in root.rglob("*.json"):
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                self._collect_video_ids(value, result)
        return result

    @classmethod
    def _collect_video_ids(cls, value: Any, result: set[str]) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"video_id", "source_video_id"} and isinstance(child, str):
                    result.add(child)
                else:
                    cls._collect_video_ids(child, result)
        elif isinstance(value, list):
            for child in value:
                cls._collect_video_ids(child, result)

    def show(self, plan_id: str) -> dict[str, Any]:
        path = self.plan_path(plan_id)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise MediaLifecycleError(f"Cleanup plan {plan_id} is corrupt.") from error
        if (
            not isinstance(value, dict) or value.get("version") != 1
            or value.get("plan_id") != plan_id
            or value.get("data_root") != str(self.data_root)
            or value.get("content_sha256") != _content_hash(value)
        ):
            raise MediaLifecycleError("Cleanup plan identity or checksum is invalid.")
        return value

    def plan_path(self, plan_id: str) -> Path:
        if not isinstance(plan_id, str) or not PLAN_ID.fullmatch(plan_id):
            raise MediaLifecycleError("Cleanup plan ID is invalid.")
        return self.plan_root / f"{plan_id}.json"

    def quarantine_path(self, quarantine_id: str) -> Path:
        if not isinstance(quarantine_id, str) or not QUARANTINE_ID.fullmatch(quarantine_id):
            raise MediaLifecycleError("Quarantine ID is invalid.")
        return self.quarantine_root / quarantine_id

    def apply(
        self, plan_id: str, *, confirm: str, execute: bool = False,
    ) -> dict[str, Any]:
        if confirm != plan_id:
            raise MediaLifecycleError("Cleanup application confirmation does not match plan ID.")
        plan = self.show(plan_id)
        if not execute:
            return {
                "status": "dry_run", "plan_id": plan_id,
                "eligible_items": sum(item["eligible"] for item in plan["items"]),
                "eligible_bytes": sum(
                    item["size_bytes"] for item in plan["items"] if item["eligible"]
                ),
            }
        with (
            self.production_lock_factory(), self.graph.queue.locked(),
            self.graph.publications.locked(), self._locked(),
        ):
            existing = self._quarantine_for_plan(plan_id)
            if existing is not None and existing.get("state") != "quarantining":
                return existing
            current = {item["relative_path"]: item for item in self.evaluate()}
            if existing is None:
                quarantine_id = f"quarantine_{uuid.uuid4().hex[:24]}"
                directory = self.quarantine_path(quarantine_id)
                manifest = {
                    "version": 1,
                    "quarantine_id": quarantine_id,
                    "plan_id": plan_id,
                    "plan_sha256": plan["content_sha256"],
                    "created_at": self.now().isoformat(),
                    "updated_at": self.now().isoformat(),
                    "state": "quarantining",
                    "items": [],
                    "quarantined_bytes": 0,
                }
            else:
                manifest = existing
                quarantine_id = manifest["quarantine_id"]
                directory = self.quarantine_path(quarantine_id)
                if manifest.get("plan_sha256") != plan["content_sha256"]:
                    raise MediaLifecycleError(
                        "Interrupted quarantine does not match the immutable plan."
                    )
            files_root = directory / "files"
            if existing is None:
                directory.mkdir(parents=True, mode=0o700)
            _atomic_json(directory / "manifest.json", manifest)
            recorded = {item["relative_path"] for item in manifest["items"]}
            for planned in plan["items"]:
                if not planned["eligible"] or planned["relative_path"] in recorded:
                    continue
                result = {
                    "relative_path": planned["relative_path"],
                    "quarantine_relative_path": (
                        "files/" + planned["relative_path"]
                    ),
                    "size_bytes": planned["size_bytes"],
                    "sha256": planned["sha256"],
                    "reason": planned["reasons"][0],
                    "status": "skipped",
                    "detail": None,
                }
                now_item = current.get(planned["relative_path"])
                destination = files_root / planned["relative_path"]
                recovered_move = False
                try:
                    recovered = self._safe_quarantined_file(destination, directory)
                    recovered_move = (
                        recovered.stat().st_size == planned["size_bytes"]
                        and sha256_file(recovered) == planned["sha256"]
                    )
                except (OSError, MediaLifecycleError):
                    pass
                if recovered_move:
                    result["status"] = "quarantined"
                    result["detail"] = "Recovered an interrupted atomic move."
                    manifest["quarantined_bytes"] += planned["size_bytes"]
                elif (
                    now_item is None or not now_item["eligible"]
                    or now_item["state_fingerprint"] != planned["state_fingerprint"]
                ):
                    result["detail"] = "Lifecycle state changed after planning."
                else:
                    try:
                        source = self._safe_file(
                            self.data_root / planned["relative_path"]
                        )
                        if (
                            source.stat().st_size != planned["size_bytes"]
                            or sha256_file(source) != planned["sha256"]
                        ):
                            raise MediaLifecycleError(
                                "Media size or checksum changed after planning."
                            )
                        self._safe_destination(destination, files_root)
                        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        os.replace(source, destination)
                        result["status"] = "quarantined"
                        manifest["quarantined_bytes"] += planned["size_bytes"]
                    except (OSError, MediaLifecycleError) as error:
                        result["detail"] = safe_publication_text(error)
                manifest["items"].append(result)
                manifest["updated_at"] = self.now().isoformat()
                _atomic_json(directory / "manifest.json", manifest)
                self._audit(
                    "item_" + result["status"], quarantine_id=quarantine_id,
                    plan_id=plan_id, relative_path=result["relative_path"],
                    size_bytes=result["size_bytes"], detail=result["detail"],
                )
            manifest["state"] = "quarantined"
            manifest["updated_at"] = self.now().isoformat()
            _atomic_json(directory / "manifest.json", manifest)
            self._audit(
                "quarantine_completed", quarantine_id=quarantine_id,
                plan_id=plan_id, bytes=manifest["quarantined_bytes"],
            )
            return copy.deepcopy(manifest)

    def restore(self, quarantine_id: str) -> dict[str, Any]:
        with (
            self.production_lock_factory(), self.graph.queue.locked(),
            self.graph.publications.locked(), self._locked(),
        ):
            directory = self.quarantine_path(quarantine_id)
            manifest = self._read_quarantine(directory, quarantine_id)
            for item in manifest["items"]:
                if item["status"] == "restored":
                    continue
                if item["status"] != "quarantined":
                    continue
                try:
                    source = self._safe_quarantined_file(
                        directory / item["quarantine_relative_path"], directory
                    )
                    destination = self.data_root / item["relative_path"]
                    self._safe_destination(destination, self.data_root)
                    if destination.exists() or destination.is_symlink():
                        raise MediaLifecycleError(
                            f"Restore destination is occupied: {item['relative_path']}."
                        )
                    if (
                        source.stat().st_size != item["size_bytes"]
                        or sha256_file(source) != item["sha256"]
                    ):
                        raise MediaLifecycleError(
                            f"Quarantined checksum changed: {item['relative_path']}."
                        )
                    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    os.replace(source, destination)
                    item["status"] = "restored"
                    item["detail"] = None
                except (OSError, MediaLifecycleError) as error:
                    item["detail"] = safe_publication_text(error)
                manifest["updated_at"] = self.now().isoformat()
                _atomic_json(directory / "manifest.json", manifest)
                self._audit(
                    "item_restored" if item["status"] == "restored" else "restore_skipped",
                    quarantine_id=quarantine_id,
                    relative_path=item["relative_path"],
                    detail=item["detail"],
                )
            manifest["state"] = (
                "restored" if not any(
                    item["status"] == "quarantined" for item in manifest["items"]
                ) else "partially_restored"
            )
            manifest["updated_at"] = self.now().isoformat()
            _atomic_json(directory / "manifest.json", manifest)
            return copy.deepcopy(manifest)

    def purge(
        self, quarantine_id: str, *, confirm: str,
    ) -> dict[str, Any]:
        if confirm != quarantine_id:
            raise MediaLifecycleError("Purge confirmation does not match quarantine ID.")
        with self.production_lock_factory(), self._locked():
            directory = self.quarantine_path(quarantine_id)
            manifest = self._read_quarantine(directory, quarantine_id)
            if manifest["state"] == "purged":
                return manifest
            eligible_at = _parse_time(manifest["created_at"]) + timedelta(
                days=self.policy.quarantine_retention_days
            )
            if self.now().astimezone(timezone.utc) < eligible_at:
                raise MediaLifecycleError(
                    f"Quarantine purge is blocked until {eligible_at.isoformat()}."
                )
            purged_bytes = 0
            for item in manifest["items"]:
                if item["status"] != "quarantined":
                    continue
                try:
                    path = self._safe_quarantined_file(
                        directory / item["quarantine_relative_path"], directory
                    )
                    if sha256_file(path) != item["sha256"]:
                        raise MediaLifecycleError("Quarantined media checksum changed.")
                    path.unlink()
                    purged_bytes += item["size_bytes"]
                    item["status"] = "purged"
                    item["detail"] = None
                except (OSError, MediaLifecycleError) as error:
                    item["detail"] = safe_publication_text(error)
                    self._audit(
                        "purge_skipped", quarantine_id=quarantine_id,
                        relative_path=item["relative_path"], detail=item["detail"],
                    )
            manifest["state"] = (
                "purged" if not any(
                    item["status"] == "quarantined" for item in manifest["items"]
                ) else "partially_purged"
            )
            manifest["purged_at"] = self.now().isoformat()
            manifest["purged_bytes"] = purged_bytes
            manifest["updated_at"] = manifest["purged_at"]
            _atomic_json(directory / "manifest.json", manifest)
            self._audit(
                "quarantine_purged", quarantine_id=quarantine_id,
                bytes=purged_bytes,
            )
            return copy.deepcopy(manifest)

    def run_eligible(self, *, execute: bool = False) -> dict[str, Any]:
        plan = self.plan()
        applied = self.apply(
            plan["plan_id"], confirm=plan["plan_id"], execute=execute
        )
        return {"plan": plan, "application": applied}

    def status(self) -> dict[str, Any]:
        quarantines = []
        if self.quarantine_root.is_dir():
            for directory in sorted(self.quarantine_root.glob("quarantine_*")):
                try:
                    quarantines.append(self._read_quarantine(directory, directory.name))
                except (OSError, MediaLifecycleError, json.JSONDecodeError):
                    continue
        return {
            "quarantine_count": len(quarantines),
            "quarantined_bytes": sum(
                item.get("quarantined_bytes", 0) for item in quarantines
                if item.get("state") == "quarantined"
            ),
            "latest_quarantine": max(
                (item.get("updated_at") for item in quarantines), default=None
            ),
        }

    def _safe_file(self, path: Path) -> Path:
        path = Path(path)
        if path.is_symlink():
            raise MediaLifecycleError("Cleanup refuses symbolic links.")
        try:
            lexical = Path(os.path.abspath(path))
            lexical.relative_to(self.data_root)
            current = self.data_root
            for part in lexical.relative_to(self.data_root).parts:
                current = current / part
                if current.is_symlink():
                    raise MediaLifecycleError("Cleanup refuses symbolic links.")
            resolved = lexical.resolve(strict=True)
            resolved.relative_to(self.data_root)
        except (OSError, ValueError) as error:
            raise MediaLifecycleError("Media path escapes the persistent data root.") from error
        info = resolved.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise MediaLifecycleError("Cleanup accepts regular files only.")
        if info.st_uid != self.owner_uid:
            raise MediaLifecycleError("Media ownership does not match the service user.")
        return resolved

    @staticmethod
    def _safe_destination(path: Path, root: Path) -> None:
        root = Path(root).resolve()
        candidate = Path(path)
        if candidate.is_absolute():
            try:
                candidate.relative_to(root)
            except ValueError as error:
                raise MediaLifecycleError("Destination escapes its protected root.") from error
        current = root
        relative = candidate.relative_to(root) if candidate.is_absolute() else candidate
        for part in relative.parts:
            if part in {"", ".", ".."}:
                raise MediaLifecycleError("Destination contains path traversal.")
            current = current / part
            if current.exists() and current.is_symlink():
                raise MediaLifecycleError("Destination contains a symbolic link.")

    def _safe_quarantined_file(self, path: Path, directory: Path) -> Path:
        path = Path(path)
        protected_root = directory.resolve()
        lexical = Path(os.path.abspath(path))
        try:
            relative = lexical.relative_to(protected_root)
        except ValueError as error:
            raise MediaLifecycleError(
                "Quarantine path escapes its protected root."
            ) from error
        current = protected_root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise MediaLifecycleError("Quarantine contains a symbolic link.")
        try:
            resolved = lexical.resolve(strict=True)
            resolved.relative_to(protected_root)
        except (OSError, ValueError) as error:
            raise MediaLifecycleError("Quarantine path escapes its protected root.") from error
        if resolved.is_symlink() or not resolved.is_file():
            raise MediaLifecycleError("Quarantine contains an unsafe file.")
        if resolved.stat().st_uid != self.owner_uid:
            raise MediaLifecycleError("Quarantine file ownership is unexpected.")
        return resolved

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.lock_path.open("a+") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _quarantine_for_plan(self, plan_id: str) -> dict[str, Any] | None:
        if not self.quarantine_root.is_dir():
            return None
        for directory in self.quarantine_root.glob("quarantine_*"):
            try:
                value = self._read_quarantine(directory, directory.name)
            except (OSError, MediaLifecycleError, json.JSONDecodeError):
                continue
            if value["plan_id"] == plan_id:
                return value
        return None

    @staticmethod
    def _read_quarantine(directory: Path, quarantine_id: str) -> dict[str, Any]:
        value = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if (
            not isinstance(value, dict) or value.get("version") != 1
            or value.get("quarantine_id") != quarantine_id
            or not isinstance(value.get("items"), list)
        ):
            raise MediaLifecycleError("Quarantine manifest is malformed.")
        return value

    def _audit(self, event: str, **fields: Any) -> None:
        safe = {
            "version": 1, "timestamp": utc_now(),
            "event": safe_publication_text(event, maximum=100),
        }
        for key, value in fields.items():
            if key in {"token", "secret", "authorization", "cookie", "contents"}:
                continue
            safe[key] = (
                safe_publication_text(value) if isinstance(value, str) else value
            )
        self.audit_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.audit_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(safe, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
