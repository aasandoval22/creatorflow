"""Durable, platform-neutral publication lifecycle and sanitized audit state."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import tempfile
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Mapping

from backend.services.video_manifest import utc_now


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PUBLICATION_ROOT = PROJECT_ROOT / "data" / "publication"
DEFAULT_PUBLICATION_PATH = DEFAULT_PUBLICATION_ROOT / "records.json"
DEFAULT_PUBLICATION_AUDIT_PATH = DEFAULT_PUBLICATION_ROOT / "events.jsonl"
PUBLICATION_VERSION = 1
ATTEMPT_ID = re.compile(r"publication_[0-9a-f]{24}")
SHA256 = re.compile(r"[0-9a-f]{64}")


class PublicationError(ValueError):
    """Publication state is invalid or cannot be changed safely."""


class PublicationState(str, Enum):
    NOT_APPROVED = "not_approved"
    APPROVED = "approved"
    PUBLISH_READY = "publish_ready"
    AWAITING_CONSENT = "awaiting_consent"
    QUEUED = "queued"
    INITIALIZING = "initializing"
    TRANSFERRING = "transferring"
    INBOX_DELIVERED = "inbox_delivered"
    AWAITING_CREATOR_POST = "awaiting_creator_post"
    PROCESSING = "processing"
    PUBLISH_COMPLETE = "publish_complete"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"
    CANCELLED = "cancelled"


TERMINAL_STATES = frozenset({
    PublicationState.PUBLISH_COMPLETE.value,
    PublicationState.FAILED_TERMINAL.value,
    PublicationState.CANCELLED.value,
})
UNRESOLVED_STATES = frozenset(
    state.value for state in PublicationState if state.value not in TERMINAL_STATES
)
CLEANUP_BLOCKING_STATES = frozenset(
    state.value for state in PublicationState
    if state is not PublicationState.PUBLISH_COMPLETE
)
TRANSITIONS = {
    PublicationState.NOT_APPROVED.value: {PublicationState.APPROVED.value},
    PublicationState.APPROVED.value: {PublicationState.PUBLISH_READY.value},
    PublicationState.PUBLISH_READY.value: {PublicationState.AWAITING_CONSENT.value},
    PublicationState.AWAITING_CONSENT.value: {
        PublicationState.QUEUED.value,
        PublicationState.CANCELLED.value,
    },
    PublicationState.QUEUED.value: {
        PublicationState.INITIALIZING.value,
        PublicationState.CANCELLED.value,
    },
    PublicationState.INITIALIZING.value: {
        PublicationState.TRANSFERRING.value,
        PublicationState.PROCESSING.value,
        PublicationState.FAILED_RETRYABLE.value,
        PublicationState.FAILED_TERMINAL.value,
    },
    PublicationState.TRANSFERRING.value: {
        PublicationState.INBOX_DELIVERED.value,
        PublicationState.PROCESSING.value,
        PublicationState.FAILED_RETRYABLE.value,
        PublicationState.FAILED_TERMINAL.value,
    },
    PublicationState.INBOX_DELIVERED.value: {
        PublicationState.AWAITING_CREATOR_POST.value,
        PublicationState.PROCESSING.value,
        PublicationState.PUBLISH_COMPLETE.value,
        PublicationState.FAILED_RETRYABLE.value,
        PublicationState.FAILED_TERMINAL.value,
    },
    PublicationState.AWAITING_CREATOR_POST.value: {
        PublicationState.PROCESSING.value,
        PublicationState.PUBLISH_COMPLETE.value,
        PublicationState.FAILED_RETRYABLE.value,
        PublicationState.FAILED_TERMINAL.value,
    },
    PublicationState.PROCESSING.value: {
        PublicationState.INBOX_DELIVERED.value,
        PublicationState.AWAITING_CREATOR_POST.value,
        PublicationState.PUBLISH_COMPLETE.value,
        PublicationState.FAILED_RETRYABLE.value,
        PublicationState.FAILED_TERMINAL.value,
    },
    PublicationState.FAILED_RETRYABLE.value: {
        PublicationState.QUEUED.value,
        PublicationState.PROCESSING.value,
        PublicationState.INBOX_DELIVERED.value,
        PublicationState.AWAITING_CREATOR_POST.value,
        PublicationState.PUBLISH_COMPLETE.value,
        PublicationState.FAILED_TERMINAL.value,
        PublicationState.CANCELLED.value,
    },
    PublicationState.PUBLISH_COMPLETE.value: set(),
    PublicationState.FAILED_TERMINAL.value: set(),
    PublicationState.CANCELLED.value: set(),
}

ATTEMPT_FIELDS = {
    "attempt_id", "review_id", "source_video_id", "candidate_id",
    "timing_revision", "rendered_media_path", "rendered_media_sha256",
    "platform", "destination_account_id", "destination_account_name",
    "caption", "source_attribution", "rights_confirmed_at",
    "state", "stale", "stale_reason", "idempotency_key", "transport",
    "remote_publish_id", "remote_post_ids", "share_urls", "error_reason",
    "created_at", "updated_at", "consented_at", "inbox_delivered_at",
    "publish_completed_at", "last_status_at", "next_reconcile_at",
    "transfer_uncertain", "retry_count",
}

SECRET_PATTERN = re.compile(
    r"(?i)(access[_-]?token|refresh[_-]?token|upload[_-]?token|token|client[_-]?secret|authorization|"
    r"cookie|code|state)\s*[:=]\s*[^\s,;]+"
)
URL_PATTERN = re.compile(r"https?://[^\s]+", re.IGNORECASE)


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def safe_publication_text(value: Any, *, maximum: int = 500) -> str:
    """Return bounded audit/display text without likely credentials or URLs."""

    text = str(value or "").replace("\x00", " ")
    text = SECRET_PATTERN.sub(r"\1=<redacted>", text)
    text = URL_PATTERN.sub("<redacted-url>", text)
    text = " ".join(text.split())
    return text[:maximum]


def _timestamp(value: Any, label: str, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str) or not value:
        raise PublicationError(f"{label} must be a UTC ISO timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise PublicationError(f"{label} must be a UTC ISO timestamp.") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise PublicationError(f"{label} must include UTC timezone information.")


def _text(value: Any, label: str, *, maximum: int, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        suffix = " or null" if nullable else ""
        raise PublicationError(
            f"{label} must be a nonempty string of at most {maximum} characters{suffix}."
        )
    if any(ord(character) < 32 and character not in "\n\t" for character in value):
        raise PublicationError(f"{label} contains unsupported control characters.")


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


class PublicationStore:
    """Strict atomic publication records plus append-only sanitized history."""

    _registry_guard = threading.Lock()
    _process_locks: dict[Path, threading.RLock] = {}
    _local = threading.local()

    def __init__(
        self, path: Path = DEFAULT_PUBLICATION_PATH,
        audit_path: Path = DEFAULT_PUBLICATION_AUDIT_PATH,
    ) -> None:
        self.path = Path(path)
        self.audit_path = Path(audit_path)
        self.lock_path = self.path.with_name(f"{self.path.name}.lock")
        key = self.path.resolve()
        with self._registry_guard:
            self._process_lock = self._process_locks.setdefault(
                key, threading.RLock()
            )
        if self.path.exists():
            self._validate(self._read())

    @staticmethod
    def empty() -> dict[str, Any]:
        return {"version": PUBLICATION_VERSION, "updated_at": utc_now(), "attempts": []}

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Serialize publication transactions in-process and across processes."""

        with self._process_lock:
            key = str(self.path.resolve())
            depths = getattr(self._local, "depths", {})
            depth = depths.get(key, 0)
            stream = None
            try:
                if depth == 0:
                    self.lock_path.parent.mkdir(
                        parents=True, exist_ok=True, mode=0o700
                    )
                    stream = self.lock_path.open("a+")
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                depths[key] = depth + 1
                self._local.depths = depths
                yield
            finally:
                if depth == 0:
                    depths.pop(key, None)
                    if stream is not None:
                        try:
                            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                        finally:
                            stream.close()
                else:
                    depths[key] = depth

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return self.empty()
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise PublicationError(
                f"Publication state {self.path} is corrupt: {error}."
            ) from error
        self._validate(value)
        return value

    def _write(self, document: dict[str, Any]) -> None:
        document = copy.deepcopy(document)
        document["updated_at"] = utc_now()
        self._validate(document)
        _atomic_json(self.path, document)

    def _validate(self, document: Any) -> None:
        if (
            not isinstance(document, dict)
            or set(document) != {"version", "updated_at", "attempts"}
            or document.get("version") != PUBLICATION_VERSION
            or not isinstance(document.get("attempts"), list)
        ):
            raise PublicationError(
                "Publication state must contain version 1, updated_at, and attempts."
            )
        _timestamp(document["updated_at"], "Publication updated_at")
        attempts: set[str] = set()
        idempotency: set[str] = set()
        for index, attempt in enumerate(document["attempts"]):
            self._validate_attempt(attempt, index)
            if attempt["attempt_id"] in attempts:
                raise PublicationError("Publication state contains duplicate attempt IDs.")
            if attempt["idempotency_key"] in idempotency:
                raise PublicationError("Publication state contains duplicate idempotency keys.")
            attempts.add(attempt["attempt_id"])
            idempotency.add(attempt["idempotency_key"])

    @staticmethod
    def _validate_attempt(attempt: Any, index: int = 0) -> None:
        label = f"Publication attempt {index}"
        if not isinstance(attempt, dict) or set(attempt) != ATTEMPT_FIELDS:
            raise PublicationError(f"{label} has missing or unknown fields.")
        if not ATTEMPT_ID.fullmatch(str(attempt["attempt_id"])):
            raise PublicationError(f"{label} attempt_id is invalid.")
        for field, maximum in (
            ("review_id", 128), ("source_video_id", 128), ("candidate_id", 160),
            ("rendered_media_path", 4096), ("platform", 40),
            ("destination_account_id", 256), ("destination_account_name", 256),
            ("caption", 4000), ("source_attribution", 1000),
            ("idempotency_key", 128), ("transport", 40),
        ):
            _text(attempt[field], f"{label} {field}", maximum=maximum)
        if attempt["platform"] not in {"tiktok", "youtube_shorts", "instagram_reels"}:
            raise PublicationError(f"{label} platform is unsupported.")
        if attempt["transport"] not in {"FILE_UPLOAD", "PULL_FROM_URL"}:
            raise PublicationError(f"{label} transport is unsupported.")
        revision = attempt["timing_revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise PublicationError(f"{label} timing_revision is invalid.")
        retry_count = attempt["retry_count"]
        if isinstance(retry_count, bool) or not isinstance(retry_count, int) or retry_count < 0:
            raise PublicationError(f"{label} retry_count is invalid.")
        if not SHA256.fullmatch(str(attempt["rendered_media_sha256"])):
            raise PublicationError(f"{label} rendered-media checksum is invalid.")
        if not SHA256.fullmatch(str(attempt["idempotency_key"])):
            raise PublicationError(f"{label} idempotency key is invalid.")
        if attempt["state"] not in {state.value for state in PublicationState}:
            raise PublicationError(f"{label} publication state is unsupported.")
        for field in ("stale", "transfer_uncertain"):
            if not isinstance(attempt[field], bool):
                raise PublicationError(f"{label} {field} must be boolean.")
        for field in (
            "rights_confirmed_at", "created_at", "updated_at", "consented_at",
            "inbox_delivered_at", "publish_completed_at", "last_status_at",
            "next_reconcile_at",
        ):
            _timestamp(
                attempt[field], f"{label} {field}",
                nullable=field not in {"rights_confirmed_at", "created_at", "updated_at"},
            )
        for field, maximum in (
            ("stale_reason", 500), ("remote_publish_id", 256),
            ("error_reason", 500),
        ):
            _text(attempt[field], f"{label} {field}", maximum=maximum, nullable=True)
        for field in ("remote_post_ids", "share_urls"):
            if not isinstance(attempt[field], list) or any(
                not isinstance(value, str) or not value.strip() or len(value) > 2048
                for value in attempt[field]
            ):
                raise PublicationError(f"{label} {field} must be a list of strings.")
        if attempt["state"] == PublicationState.PUBLISH_COMPLETE.value:
            if attempt["publish_completed_at"] is None:
                raise PublicationError(f"{label} completed publication needs a timestamp.")
        if attempt["stale"] and not attempt["stale_reason"]:
            raise PublicationError(f"{label} stale attempts require a reason.")

    def list_attempts(
        self, *, review_id: str | None = None, platform: str | None = None,
    ) -> list[dict[str, Any]]:
        with self.locked():
            attempts = self._read()["attempts"]
        return copy.deepcopy([
            attempt for attempt in attempts
            if (review_id is None or attempt["review_id"] == review_id)
            and (platform is None or attempt["platform"] == platform)
        ])

    def get(self, attempt_id: str) -> dict[str, Any]:
        attempt = next(
            (item for item in self.list_attempts() if item["attempt_id"] == attempt_id),
            None,
        )
        if attempt is None:
            raise PublicationError(f"Publication attempt {attempt_id!r} was not found.")
        return attempt

    def latest_for_review(
        self, review_id: str, *, platform: str = "tiktok",
    ) -> dict[str, Any] | None:
        attempts = self.list_attempts(review_id=review_id, platform=platform)
        return max(attempts, key=lambda item: item["created_at"]) if attempts else None

    @staticmethod
    def idempotency_key(
        review_id: str, timing_revision: int, checksum: str,
        platform: str, destination_account_id: str,
    ) -> str:
        payload = "\0".join((
            review_id, str(timing_revision), checksum,
            platform, destination_account_id,
        )).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def prepare(
        self, *, review: Mapping[str, Any], media_path: Path,
        media_sha256: str, platform: str, destination_account_id: str,
        destination_account_name: str, caption: str,
        source_attribution: str, transport: str,
        rights_confirmed: bool,
    ) -> dict[str, Any]:
        if review.get("status") != "approved":
            raise PublicationError("Only an approved review can be prepared for publication.")
        if not rights_confirmed:
            raise PublicationError(
                "Publication preparation requires an explicit rights confirmation."
            )
        if not Path(media_path).is_file():
            raise PublicationError("The approved rendered media is unavailable.")
        if sha256_file(media_path) != media_sha256:
            raise PublicationError("The rendered-media checksum changed during preparation.")
        now = utc_now()
        key = self.idempotency_key(
            str(review["review_id"]), int(review["timing_revision"]), media_sha256,
            platform, destination_account_id,
        )
        with self.locked():
            document = self._read()
            existing = next(
                (item for item in document["attempts"] if item["idempotency_key"] == key),
                None,
            )
            if existing is not None:
                if existing["state"] != PublicationState.AWAITING_CONSENT.value:
                    return copy.deepcopy(existing)
                existing.update(
                    caption=caption,
                    source_attribution=source_attribution,
                    rights_confirmed_at=now,
                    stale=False,
                    stale_reason=None,
                    updated_at=now,
                )
                self._write(document)
                self._audit(existing, "preparation_updated")
                return copy.deepcopy(existing)
            attempt = {
                "attempt_id": f"publication_{uuid.uuid4().hex[:24]}",
                "review_id": str(review["review_id"]),
                "source_video_id": str(review["video_id"]),
                "candidate_id": str(review["candidate_id"]),
                "timing_revision": int(review["timing_revision"]),
                "rendered_media_path": str(Path(media_path)),
                "rendered_media_sha256": media_sha256,
                "platform": platform,
                "destination_account_id": destination_account_id,
                "destination_account_name": destination_account_name,
                "caption": caption,
                "source_attribution": source_attribution,
                "rights_confirmed_at": now,
                "state": PublicationState.AWAITING_CONSENT.value,
                "stale": False,
                "stale_reason": None,
                "idempotency_key": key,
                "transport": transport,
                "remote_publish_id": None,
                "remote_post_ids": [],
                "share_urls": [],
                "error_reason": None,
                "created_at": now,
                "updated_at": now,
                "consented_at": None,
                "inbox_delivered_at": None,
                "publish_completed_at": None,
                "last_status_at": None,
                "next_reconcile_at": None,
                "transfer_uncertain": False,
                "retry_count": 0,
            }
            self._validate_attempt(attempt)
            document["attempts"].append(attempt)
            self._write(document)
            self._audit(attempt, "prepared")
            return copy.deepcopy(attempt)

    def transition(
        self, attempt_id: str, state: PublicationState | str, *,
        error_reason: str | None = None, remote_publish_id: str | None = None,
        remote_post_ids: list[str] | None = None,
        share_urls: list[str] | None = None,
        transfer_uncertain: bool | None = None,
        next_reconcile_at: str | None = None,
        increment_retry: bool = False,
        event: str | None = None,
    ) -> dict[str, Any]:
        target = PublicationState(state).value
        with self.locked():
            document = self._read()
            attempt = next(
                (item for item in document["attempts"] if item["attempt_id"] == attempt_id),
                None,
            )
            if attempt is None:
                raise PublicationError(f"Publication attempt {attempt_id!r} was not found.")
            current = attempt["state"]
            if target != current and target not in TRANSITIONS[current]:
                raise PublicationError(
                    f"Publication transition {current!r} to {target!r} is not allowed."
                )
            now = utc_now()
            attempt["state"] = target
            attempt["updated_at"] = now
            attempt["error_reason"] = (
                safe_publication_text(error_reason) if error_reason else None
            )
            if remote_publish_id is not None:
                _text(remote_publish_id, "Remote publish ID", maximum=256)
                attempt["remote_publish_id"] = safe_publication_text(
                    remote_publish_id, maximum=256
                )
            if remote_post_ids is not None:
                attempt["remote_post_ids"] = list(remote_post_ids)
            if share_urls is not None:
                attempt["share_urls"] = list(share_urls)
            if transfer_uncertain is not None:
                attempt["transfer_uncertain"] = transfer_uncertain
            if next_reconcile_at is not None:
                _timestamp(next_reconcile_at, "Next reconciliation")
                attempt["next_reconcile_at"] = next_reconcile_at
            if increment_retry:
                attempt["retry_count"] += 1
            if target == PublicationState.QUEUED.value and attempt["consented_at"] is None:
                attempt["consented_at"] = now
            if target in {
                PublicationState.INBOX_DELIVERED.value,
                PublicationState.AWAITING_CREATOR_POST.value,
            } and attempt["inbox_delivered_at"] is None:
                attempt["inbox_delivered_at"] = now
            if target == PublicationState.PUBLISH_COMPLETE.value:
                attempt["publish_completed_at"] = now
                attempt["next_reconcile_at"] = None
                attempt["transfer_uncertain"] = False
            if target in TERMINAL_STATES:
                attempt["next_reconcile_at"] = None
            self._write(document)
            self._audit(attempt, event or f"state_{target}")
            return copy.deepcopy(attempt)

    def record_status_check(self, attempt_id: str) -> dict[str, Any]:
        with self.locked():
            document = self._read()
            attempt = next(
                (item for item in document["attempts"] if item["attempt_id"] == attempt_id),
                None,
            )
            if attempt is None:
                raise PublicationError(f"Publication attempt {attempt_id!r} was not found.")
            attempt["last_status_at"] = utc_now()
            attempt["updated_at"] = attempt["last_status_at"]
            self._write(document)
            return copy.deepcopy(attempt)

    def mark_stale(self, review_id: str, reason: str) -> int:
        safe_reason = safe_publication_text(reason)
        if not safe_reason:
            raise PublicationError("A stale-publication reason is required.")
        changed: list[dict[str, Any]] = []
        with self.locked():
            document = self._read()
            for attempt in document["attempts"]:
                if (
                    attempt["review_id"] == review_id
                    and attempt["state"] != PublicationState.PUBLISH_COMPLETE.value
                    and not attempt["stale"]
                ):
                    attempt.update(
                        stale=True, stale_reason=safe_reason, updated_at=utc_now(),
                    )
                    changed.append(copy.deepcopy(attempt))
            if changed:
                self._write(document)
        for attempt in changed:
            self._audit(attempt, "render_stale")
        return len(changed)

    def assert_fresh(
        self, attempt: Mapping[str, Any], review: Mapping[str, Any],
    ) -> None:
        if attempt.get("stale"):
            raise PublicationError("The prepared publication is stale; prepare it again.")
        if review.get("status") != "approved":
            raise PublicationError("The clip is no longer approved.")
        if (
            attempt.get("review_id") != review.get("review_id")
            or attempt.get("source_video_id") != review.get("video_id")
            or attempt.get("candidate_id") != review.get("candidate_id")
            or attempt.get("timing_revision") != review.get("timing_revision")
        ):
            raise PublicationError("The review identity or timing revision changed.")
        path = Path(str(review.get("preview_path") or ""))
        if not path.is_file():
            raise PublicationError("The approved rendered media is unavailable.")
        if str(path) != attempt.get("rendered_media_path"):
            raise PublicationError("The approved rendered-media path changed.")
        if sha256_file(path) != attempt.get("rendered_media_sha256"):
            raise PublicationError("The approved rendered-media checksum changed.")

    def successful_attempt(
        self, review_id: str, timing_revision: int, checksum: str,
    ) -> dict[str, Any] | None:
        return next((
            attempt for attempt in self.list_attempts(review_id=review_id)
            if attempt["state"] == PublicationState.PUBLISH_COMPLETE.value
            and not attempt["stale"]
            and attempt["timing_revision"] == timing_revision
            and attempt["rendered_media_sha256"] == checksum
        ), None)

    def unresolved_count(self, platform: str, destination_account_id: str) -> int:
        return sum(
            attempt["state"] in UNRESOLVED_STATES
            for attempt in self.list_attempts(platform=platform)
            if attempt["destination_account_id"] == destination_account_id
        )

    def daily_count(
        self, platform: str, destination_account_id: str, day: str,
    ) -> int:
        return sum(
            bool(attempt["consented_at"] and attempt["consented_at"][:10] == day)
            for attempt in self.list_attempts(platform=platform)
            if attempt["destination_account_id"] == destination_account_id
        )

    def _audit(self, attempt: Mapping[str, Any], event: str) -> None:
        value = {
            "version": 1,
            "timestamp": utc_now(),
            "event": safe_publication_text(event, maximum=100),
            "attempt_id": attempt["attempt_id"],
            "review_id": attempt["review_id"],
            "source_video_id": attempt["source_video_id"],
            "candidate_id": attempt["candidate_id"],
            "platform": attempt["platform"],
            "state": attempt["state"],
            "stale": attempt["stale"],
            "rights_confirmed_at": attempt["rights_confirmed_at"],
            "error_reason": safe_publication_text(attempt.get("error_reason")) or None,
            "remote_publish_id": attempt.get("remote_publish_id"),
        }
        self.audit_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        serialized = json.dumps(value, sort_keys=True, ensure_ascii=False)
        with self.audit_path.open("a", encoding="utf-8") as stream:
            stream.write(serialized + "\n")
            stream.flush()
            os.fsync(stream.fileno())
