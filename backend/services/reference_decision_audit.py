"""Durable, sanitized audit history for reference-candidate decisions."""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from backend.services.video_manifest import utc_now


AUDIT_VERSION = 1
AUDIT_ACTIONS = frozenset(
    {"accept", "reject", "duplicate", "reconsider", "withdraw"}
)
AUDIT_RESULTS = frozenset({"success", "failure"})
EVENT_FIELDS = {
    "version",
    "event_id",
    "timestamp",
    "video_id",
    "action",
    "previous_status",
    "requested_status",
    "resulting_status",
    "previous_revision",
    "resulting_revision",
    "accepted_reference_id_before",
    "accepted_reference_id_after",
    "result",
    "failure_reason",
    "reviewer",
    "note",
    "request_id",
    "recovery_key",
}
SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
MAX_AUDIT_TEXT = 4_000


class ReferenceDecisionAuditError(RuntimeError):
    """The local decision ledger is malformed or could not be persisted."""


def safe_audit_text(
    value: str | None, *, maximum: int = MAX_AUDIT_TEXT
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReferenceDecisionAuditError("Audit text values must be strings.")
    cleaned = "".join(
        character if character >= " " and character != "\x7f" else " "
        for character in value
    ).strip()
    if len(cleaned) > maximum:
        raise ReferenceDecisionAuditError(
            f"Audit text values are limited to {maximum} characters."
        )
    return cleaned or None


def configured_reviewer_name(value: str | None = None) -> str | None:
    """Return an optional display label, never an authentication assertion."""

    configured = os.environ.get("AUTOCLIP_REVIEWER_NAME") if value is None else value
    return safe_audit_text(configured, maximum=100)


class ReferenceDecisionAuditLedger:
    """Append-only JSONL ledger with strict reads and durable single-line appends."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def history(
        self, video_id: str | None = None, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        if video_id is not None and not SAFE_ID.fullmatch(video_id):
            raise ReferenceDecisionAuditError("Candidate video ID is invalid.")
        if limit is not None and limit < 1:
            raise ReferenceDecisionAuditError("History limit must be positive.")
        if not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        try:
            with self.path.open(encoding="utf-8") as stream:
                for line_number, line in enumerate(stream, start=1):
                    if not line.strip():
                        raise ReferenceDecisionAuditError(
                            f"Decision ledger {self.path} has a blank line at "
                            f"{line_number}; repair it before continuing."
                        )
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError as error:
                        raise ReferenceDecisionAuditError(
                            f"Decision ledger {self.path} is corrupt at line "
                            f"{line_number}: {error}."
                        ) from error
                    self._validate_event(event, line_number=line_number)
                    if video_id is None or event["video_id"] == video_id:
                        events.append(event)
        except OSError as error:
            raise ReferenceDecisionAuditError(
                f"Cannot read decision ledger {self.path}: {error}."
            ) from error
        return events[-limit:] if limit is not None else events

    def append(self, event: Mapping[str, Any]) -> dict[str, Any]:
        document = dict(event)
        self._validate_event(document)
        encoded = (
            json.dumps(
                document,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.path.exists()
        previous_size = self.path.stat().st_size if existed else 0
        descriptor: int | None = None
        try:
            descriptor = os.open(
                self.path,
                os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                0o600,
            )
            written = os.write(descriptor, encoded)
            if written != len(encoded):
                raise OSError(
                    f"short audit write: expected {len(encoded)}, wrote {written}"
                )
            os.fsync(descriptor)
            if not existed:
                directory = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except OSError as error:
            if descriptor is not None:
                try:
                    os.ftruncate(descriptor, previous_size)
                    os.fsync(descriptor)
                except OSError:
                    pass
            raise ReferenceDecisionAuditError(
                f"Cannot append decision audit event: {error}."
            ) from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
        return document

    @staticmethod
    def event(
        *,
        video_id: str,
        action: str,
        previous_status: str,
        requested_status: str,
        resulting_status: str,
        previous_revision: int,
        resulting_revision: int,
        accepted_reference_id_before: str | None,
        accepted_reference_id_after: str | None,
        result: str,
        failure_reason: str | None = None,
        reviewer: str | None = None,
        note: str | None = None,
        request_id: str | None = None,
        recovery_key: str | None = None,
        event_id: str | None = None,
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        return {
            "version": AUDIT_VERSION,
            "event_id": event_id or uuid.uuid4().hex,
            "timestamp": timestamp or utc_now(),
            "video_id": video_id,
            "action": action,
            "previous_status": previous_status,
            "requested_status": requested_status,
            "resulting_status": resulting_status,
            "previous_revision": previous_revision,
            "resulting_revision": resulting_revision,
            "accepted_reference_id_before": accepted_reference_id_before,
            "accepted_reference_id_after": accepted_reference_id_after,
            "result": result,
            "failure_reason": safe_audit_text(failure_reason, maximum=500),
            "reviewer": configured_reviewer_name(reviewer),
            "note": safe_audit_text(note),
            "request_id": request_id,
            "recovery_key": recovery_key,
        }

    @staticmethod
    def _validate_event(
        event: Any, *, line_number: int | None = None
    ) -> None:
        location = f" at line {line_number}" if line_number is not None else ""
        if not isinstance(event, dict) or set(event) != EVENT_FIELDS:
            raise ReferenceDecisionAuditError(
                f"Decision audit event{location} has missing or unknown fields."
            )
        if event["version"] != AUDIT_VERSION:
            raise ReferenceDecisionAuditError(
                f"Decision audit event{location} has an unsupported version."
            )
        for name in ("event_id", "video_id"):
            value = event[name]
            if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
                raise ReferenceDecisionAuditError(
                    f"Decision audit event{location} has invalid {name}."
                )
        if event["action"] not in AUDIT_ACTIONS:
            raise ReferenceDecisionAuditError(
                f"Decision audit event{location} has an unsupported action."
            )
        if event["result"] not in AUDIT_RESULTS:
            raise ReferenceDecisionAuditError(
                f"Decision audit event{location} has an unsupported result."
            )
        for name in (
            "previous_status",
            "requested_status",
            "resulting_status",
            "timestamp",
        ):
            if not isinstance(event[name], str) or not event[name]:
                raise ReferenceDecisionAuditError(
                    f"Decision audit event{location} has invalid {name}."
                )
        try:
            timestamp = datetime.fromisoformat(
                event["timestamp"].replace("Z", "+00:00")
            )
        except ValueError as error:
            raise ReferenceDecisionAuditError(
                f"Decision audit event{location} has invalid timestamp."
            ) from error
        if timestamp.tzinfo is None:
            raise ReferenceDecisionAuditError(
                f"Decision audit event{location} timestamp has no timezone."
            )
        for name in ("previous_revision", "resulting_revision"):
            if (
                isinstance(event[name], bool)
                or not isinstance(event[name], int)
                or event[name] < 0
            ):
                raise ReferenceDecisionAuditError(
                    f"Decision audit event{location} has invalid {name}."
                )
        for name in (
            "accepted_reference_id_before",
            "accepted_reference_id_after",
            "failure_reason",
            "reviewer",
            "note",
            "request_id",
            "recovery_key",
        ):
            value = event[name]
            if value is not None and not isinstance(value, str):
                raise ReferenceDecisionAuditError(
                    f"Decision audit event{location} has invalid {name}."
                )
        if event["request_id"] is not None and not SAFE_ID.fullmatch(
            event["request_id"]
        ):
            raise ReferenceDecisionAuditError(
                f"Decision audit event{location} has invalid request_id."
            )
        serialized_names = " ".join(event).casefold()
        if any(
            forbidden in serialized_names
            for forbidden in (
                "token",
                "authorization",
                "cookie",
                "api_key",
                "environment",
            )
        ):
            raise ReferenceDecisionAuditError(
                f"Decision audit event{location} contains a forbidden field."
            )
