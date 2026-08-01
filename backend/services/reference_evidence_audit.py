"""Append-only sanitized audit events for reference evidence changes."""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from backend.services.reference_decision_audit import configured_reviewer_name
from backend.services.video_manifest import utc_now


AUDIT_VERSION = 2
ACTIONS = frozenset({
    "annotation_update", "reanalyze", "profile_rebuild", "evidence_recovery",
})
RESULTS = frozenset({"success", "failure"})
SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
LEGACY_FIELDS = {
    "version", "event_id", "timestamp", "action", "reference_id",
    "profile_name", "reference_ids", "previous_annotation_revision",
    "new_annotation_revision", "previous_analysis_revision",
    "new_analysis_revision", "changed_fields", "reviewer", "result",
    "failure_reason", "request_id",
}
FIELDS = LEGACY_FIELDS | {
    "snapshot_id", "recovery_id", "resulting_annotation_state",
    "previous_profile_sha256", "new_profile_sha256", "reason",
}


class ReferenceEvidenceAuditError(RuntimeError):
    """The evidence audit ledger is malformed or cannot be persisted."""


def safe_evidence_audit_text(
    value: str | None, *, maximum: int = 500
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReferenceEvidenceAuditError("Audit text values must be strings.")
    cleaned = "".join(
        character if character >= " " and character != "\x7f" else " "
        for character in value
    ).strip()
    sensitive = re.search(
        r"(?i)\b(form_token|token|api[_-]?key|cookie|authorization|environment)"
        r"\s*[:=]",
        cleaned,
    )
    if sensitive is not None:
        # Drop the remainder instead of trying to guess where a credential ends;
        # authorization values commonly contain whitespace (for example, Bearer).
        cleaned = f"{cleaned[:sensitive.start()].rstrip()} {sensitive.group(1)}=[redacted]"
    if len(cleaned) > maximum:
        cleaned = cleaned[:maximum].rstrip()
    return cleaned or None


class ReferenceEvidenceAuditLedger:
    """Strict JSONL history with durable one-line appends."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def history(
        self,
        *,
        reference_id: str | None = None,
        profile_name: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        for value, label in ((reference_id, "reference ID"), (profile_name, "profile name")):
            if value is not None and not SAFE_ID.fullmatch(value):
                raise ReferenceEvidenceAuditError(f"Audit {label} is invalid.")
        if limit is not None and (isinstance(limit, bool) or limit < 1):
            raise ReferenceEvidenceAuditError("History limit must be positive.")
        if not self.path.exists():
            return []
        events = []
        try:
            with self.path.open(encoding="utf-8") as stream:
                for line_number, line in enumerate(stream, start=1):
                    if not line.strip():
                        raise ReferenceEvidenceAuditError(
                            f"Evidence audit ledger has a blank line at {line_number}."
                        )
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError as error:
                        raise ReferenceEvidenceAuditError(
                            f"Evidence audit ledger is corrupt at line {line_number}: {error}."
                        ) from error
                    self._validate(event)
                    if (
                        reference_id is not None
                        and event["reference_id"] != reference_id
                        and reference_id not in event["reference_ids"]
                    ):
                        continue
                    if profile_name is not None and event["profile_name"] != profile_name:
                        continue
                    events.append(event)
        except OSError as error:
            raise ReferenceEvidenceAuditError(
                f"Cannot read evidence audit ledger {self.path}: {error}."
            ) from error
        return events[-limit:] if limit is not None else events

    def append(self, event: Mapping[str, Any]) -> dict[str, Any]:
        document = dict(event)
        self._validate(document)
        encoded = (
            json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            + "\n"
        ).encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.path.exists()
        prior_size = self.path.stat().st_size if existed else 0
        descriptor: int | None = None
        try:
            descriptor = os.open(
                self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600
            )
            if os.write(descriptor, encoded) != len(encoded):
                raise OSError("short evidence audit write")
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
                    os.ftruncate(descriptor, prior_size)
                    os.fsync(descriptor)
                except OSError:
                    pass
            raise ReferenceEvidenceAuditError(
                f"Cannot append evidence audit event: {error}."
            ) from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
        return document

    @staticmethod
    def event(
        *,
        action: str,
        result: str,
        reference_id: str | None = None,
        profile_name: str | None = None,
        reference_ids: list[str] | None = None,
        previous_annotation_revision: int | None = None,
        new_annotation_revision: int | None = None,
        previous_analysis_revision: int | None = None,
        new_analysis_revision: int | None = None,
        changed_fields: list[str] | None = None,
        reviewer: str | None = None,
        failure_reason: str | None = None,
        request_id: str | None = None,
        snapshot_id: str | None = None,
        recovery_id: str | None = None,
        resulting_annotation_state: str | None = None,
        previous_profile_sha256: str | None = None,
        new_profile_sha256: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        return {
            "version": AUDIT_VERSION,
            "event_id": uuid.uuid4().hex,
            "timestamp": utc_now(),
            "action": action,
            "reference_id": reference_id,
            "profile_name": profile_name,
            "reference_ids": sorted(reference_ids or []),
            "previous_annotation_revision": previous_annotation_revision,
            "new_annotation_revision": new_annotation_revision,
            "previous_analysis_revision": previous_analysis_revision,
            "new_analysis_revision": new_analysis_revision,
            "changed_fields": sorted(changed_fields or []),
            "reviewer": configured_reviewer_name(reviewer),
            "result": result,
            "failure_reason": safe_evidence_audit_text(failure_reason),
            "request_id": request_id,
            "snapshot_id": snapshot_id,
            "recovery_id": recovery_id,
            "resulting_annotation_state": resulting_annotation_state,
            "previous_profile_sha256": previous_profile_sha256,
            "new_profile_sha256": new_profile_sha256,
            "reason": safe_evidence_audit_text(reason),
        }

    @staticmethod
    def _validate(event: Any) -> None:
        if not isinstance(event, dict) or event.get("version") not in {1, 2}:
            raise ReferenceEvidenceAuditError(
                "Evidence audit event has an unsupported version."
            )
        expected_fields = LEGACY_FIELDS if event["version"] == 1 else FIELDS
        if set(event) != expected_fields:
            raise ReferenceEvidenceAuditError(
                "Evidence audit event has missing or unknown fields."
            )
        allowed_actions = (
            ACTIONS - {"evidence_recovery"}
            if event["version"] == 1 else ACTIONS
        )
        if event["action"] not in allowed_actions:
            raise ReferenceEvidenceAuditError("Evidence audit event has invalid version or action.")
        if event["result"] not in RESULTS:
            raise ReferenceEvidenceAuditError("Evidence audit event has invalid result.")
        if not isinstance(event["event_id"], str) or not SAFE_ID.fullmatch(event["event_id"]):
            raise ReferenceEvidenceAuditError("Evidence audit event ID is invalid.")
        try:
            timestamp = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00"))
        except (AttributeError, ValueError) as error:
            raise ReferenceEvidenceAuditError("Evidence audit timestamp is invalid.") from error
        if timestamp.tzinfo is None:
            raise ReferenceEvidenceAuditError("Evidence audit timestamp has no timezone.")
        for name in ("reference_id", "profile_name", "request_id"):
            value = event[name]
            if value is not None and (
                not isinstance(value, str) or not SAFE_ID.fullmatch(value)
            ):
                raise ReferenceEvidenceAuditError(f"Evidence audit {name} is invalid.")
        if event["reference_id"] is None and event["profile_name"] is None:
            raise ReferenceEvidenceAuditError(
                "Evidence audit event requires a reference or profile."
            )
        for name in ("reference_ids", "changed_fields"):
            value = event[name]
            if not isinstance(value, list) or not all(
                isinstance(item, str) and SAFE_ID.fullmatch(item) for item in value
            ):
                raise ReferenceEvidenceAuditError(f"Evidence audit {name} is invalid.")
        for name in (
            "previous_annotation_revision", "new_annotation_revision",
            "previous_analysis_revision", "new_analysis_revision",
        ):
            value = event[name]
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ReferenceEvidenceAuditError(f"Evidence audit {name} is invalid.")
        for name in ("reviewer", "failure_reason"):
            if event[name] is not None and not isinstance(event[name], str):
                raise ReferenceEvidenceAuditError(f"Evidence audit {name} is invalid.")
        if event["version"] == 2:
            for name in ("snapshot_id", "recovery_id"):
                value = event[name]
                if value is not None and (
                    not isinstance(value, str) or not SAFE_ID.fullmatch(value)
                ):
                    raise ReferenceEvidenceAuditError(
                        f"Evidence audit {name} is invalid."
                    )
            state = event["resulting_annotation_state"]
            if state not in {None, "present", "absent"}:
                raise ReferenceEvidenceAuditError(
                    "Evidence audit resulting annotation state is invalid."
                )
            for name in ("previous_profile_sha256", "new_profile_sha256"):
                value = event[name]
                if value is not None and (
                    not isinstance(value, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", value)
                ):
                    raise ReferenceEvidenceAuditError(
                        f"Evidence audit {name} is invalid."
                    )
            if event["reason"] is not None and not isinstance(event["reason"], str):
                raise ReferenceEvidenceAuditError("Evidence audit reason is invalid.")
        forbidden = " ".join(event).casefold()
        if any(value in forbidden for value in (
            "token", "api_key", "cookie", "authorization", "environment"
        )):
            raise ReferenceEvidenceAuditError("Evidence audit event has a forbidden field.")
