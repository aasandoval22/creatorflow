"""Verified, audited recovery of local accepted-reference evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Callable

from backend.services.reference_annotations import ReferenceAnnotationStore
from backend.services.reference_clip_library import (
    PROJECT_ROOT,
    ReferenceClipError,
    ReferenceClipLibrary,
    _atomic_json,
)
from backend.services.reference_evidence_audit import (
    ReferenceEvidenceAuditError,
    safe_evidence_audit_text,
)
from backend.services.reference_evidence_service import (
    ReferenceEvidenceError,
    ReferenceEvidenceService,
    _atomic_restore,
)
from backend.services.reference_profile_builder import ReferenceProfileError


DEFAULT_RECOVERY_ROOT = PROJECT_ROOT / "data" / "reference_evidence_recovery"
PROFILE_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
StateWriter = Callable[[Path, bytes | None], None]


class ReferenceEvidenceRecoveryError(ReferenceEvidenceError):
    """A protected evidence snapshot cannot be restored safely."""


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class ReferenceEvidenceRecoveryService:
    """Restore one annotation/profile pair while preserving every other input."""

    def __init__(
        self,
        evidence: ReferenceEvidenceService,
        *,
        recovery_root: Path = DEFAULT_RECOVERY_ROOT,
        state_writer: StateWriter = _atomic_restore,
        owner_uid: int | None = None,
    ) -> None:
        self.evidence = evidence
        self.recovery_root = Path(recovery_root)
        self.state_writer = state_writer
        self.owner_uid = os.getuid() if owner_uid is None else owner_uid

    def restore(
        self,
        reference_id: str,
        profile_name: str,
        snapshot: Path,
        *,
        reason: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        recovery_id = f"recovery_{uuid.uuid4().hex}"
        snapshot_path = Path(snapshot)
        snapshot_id = "snapshot_" + hashlib.sha256(
            str(snapshot_path.absolute()).encode("utf-8")
        ).hexdigest()[:20]
        safe_reason = safe_evidence_audit_text(reason)
        if not safe_reason:
            raise ReferenceEvidenceRecoveryError(
                "Recovery requires a nonempty human-readable reason."
            )
        with self.evidence.locked():
            self.evidence._require_unused_request(request_id)
            try:
                verified = self._verify(
                    reference_id, profile_name, snapshot_path
                )
            except Exception as error:
                self._failure_event(
                    reference_id=reference_id,
                    profile_name=profile_name,
                    snapshot_id=snapshot_id,
                    recovery_id=recovery_id,
                    reason=safe_reason,
                    request_id=request_id,
                    error=error,
                )
                if isinstance(
                    error,
                    (
                        ReferenceEvidenceRecoveryError,
                        ReferenceClipError,
                        ReferenceProfileError,
                    ),
                ):
                    raise
                raise ReferenceEvidenceRecoveryError(
                    f"Snapshot verification failed safely: {error}"
                ) from error

            current_profile = verified["current_profile"]
            current_annotation = verified["current_annotation"]
            target_profile = verified["target_profile"]
            target_annotation = verified["target_annotation"]
            previous_hash = sha256_bytes(current_profile)
            target_hash = sha256_bytes(target_profile)
            current_annotation_state = (
                "present" if current_annotation is not None else "absent"
            )
            target_annotation_state = (
                "present" if target_annotation is not None else "absent"
            )
            if (
                current_profile == target_profile
                and current_annotation == target_annotation
            ):
                return {
                    "status": "already_restored",
                    "reference_id": reference_id,
                    "profile_name": profile_name,
                    "snapshot_id": snapshot_id,
                    "profile_sha256": target_hash,
                    "annotation_state": target_annotation_state,
                    "recovery_path": None,
                }

            self.recovery_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.recovery_root, 0o700)
            staging = self.recovery_root / f".{recovery_id}.tmp"
            final = self.recovery_root / recovery_id
            staging.mkdir(mode=0o700)
            profile_path = self.evidence.profile_builder.profile_path(profile_name)
            annotation_path = self.evidence.annotations.path(reference_id)
            annotation_revision = self.evidence.annotations.read(reference_id)[
                "revision"
            ]
            manifest = {
                "version": 1,
                "recovery_id": recovery_id,
                "snapshot_id": snapshot_id,
                "reference_id": reference_id,
                "profile_name": profile_name,
                "reason": safe_reason,
                "previous_annotation_revision": annotation_revision,
                "previous_annotation_state": current_annotation_state,
                "resulting_annotation_state": target_annotation_state,
                "previous_profile_sha256": previous_hash,
                "resulting_profile_sha256": target_hash,
                "result": "success",
            }
            finalized = False
            try:
                _atomic_restore(staging / "profile.before.json", current_profile)
                if current_annotation is not None:
                    _atomic_restore(staging / "annotation.before.json", current_annotation)
                else:
                    marker = staging / "annotation.before.absent"
                    marker.touch(mode=0o600)
                _atomic_json(staging / "manifest.json", manifest)

                if target_annotation is not None:
                    self.state_writer(annotation_path, target_annotation)
                elif current_annotation is not None:
                    # The active document is retained by rename in recovery
                    # storage; it is never unlinked as part of restoration.
                    os.replace(annotation_path, staging / "annotation.displaced.json")
                self.state_writer(profile_path, target_profile)
                os.replace(staging, final)
                finalized = True
                self.evidence.audit.append(
                    self.evidence.audit.event(
                        action="evidence_recovery",
                        result="success",
                        reference_id=reference_id,
                        profile_name=profile_name,
                        previous_annotation_revision=annotation_revision,
                        new_annotation_revision=(
                            json.loads(target_annotation.decode("utf-8"))["revision"]
                            if target_annotation is not None else None
                        ),
                        reviewer=self.evidence.reviewer_name,
                        request_id=request_id,
                        snapshot_id=snapshot_id,
                        recovery_id=recovery_id,
                        resulting_annotation_state=target_annotation_state,
                        previous_profile_sha256=previous_hash,
                        new_profile_sha256=target_hash,
                        reason=safe_reason,
                    )
                )
            except Exception as error:
                try:
                    _atomic_restore(profile_path, current_profile)
                    _atomic_restore(annotation_path, current_annotation)
                except Exception as rollback_error:
                    raise ReferenceEvidenceRecoveryError(
                        "Evidence recovery failed and rollback also failed; use the "
                        f"protected backup {final if finalized else staging}: "
                        f"{rollback_error}"
                    ) from error
                backup = final if finalized else staging
                manifest["result"] = "failure"
                manifest["failure_reason"] = safe_evidence_audit_text(str(error))
                try:
                    _atomic_json(backup / "manifest.json", manifest)
                except OSError:
                    pass
                self._failure_event(
                    reference_id=reference_id,
                    profile_name=profile_name,
                    snapshot_id=snapshot_id,
                    recovery_id=recovery_id,
                    reason=safe_reason,
                    request_id=request_id,
                    error=error,
                    previous_annotation_revision=annotation_revision,
                    resulting_annotation_state=current_annotation_state,
                    previous_profile_sha256=previous_hash,
                    new_profile_sha256=previous_hash,
                )
                if isinstance(
                    error,
                    (ReferenceEvidenceAuditError, ReferenceEvidenceRecoveryError),
                ):
                    raise
                raise ReferenceEvidenceRecoveryError(
                    f"Evidence recovery failed; current state was restored: {error}"
                ) from error
            return {
                "status": "restored",
                "reference_id": reference_id,
                "profile_name": profile_name,
                "snapshot_id": snapshot_id,
                "profile_sha256": target_hash,
                "annotation_state": target_annotation_state,
                "recovery_path": str(final),
            }

    def _verify(
        self, reference_id: str, profile_name: str, snapshot: Path
    ) -> dict[str, bytes | None]:
        self._protected_directory(snapshot)
        entry = self.evidence.accepted_entry(reference_id)
        if entry["profile_name"] != profile_name:
            raise ReferenceEvidenceRecoveryError(
                "Reference category does not match the requested recovery profile."
            )
        index_path = self._protected_file(snapshot / "index.json")
        snapshot_library = ReferenceClipLibrary(
            self.evidence.library.root, index_path
        )
        snapshot_entry = snapshot_library.get(reference_id)
        if snapshot_entry != entry:
            raise ReferenceEvidenceRecoveryError(
                "Snapshot strict-index identity does not match the current reference."
            )
        self.evidence.library.validate_checksum(reference_id)
        if snapshot_entry["checksum_sha256"] != entry["checksum_sha256"]:
            raise ReferenceEvidenceRecoveryError(
                "Snapshot media checksum does not match the current reference."
            )

        snapshot_profile_path = self._protected_file(
            snapshot / "reference_profiles" / f"{profile_name}.json"
        )
        target_profile = snapshot_profile_path.read_bytes()
        target_document = self._profile_document(
            target_profile, profile_name, reference_id, "snapshot"
        )
        expected_hash = self._snapshot_profile_hash(snapshot, profile_name)
        actual_hash = sha256_bytes(target_profile)
        if actual_hash != expected_hash:
            raise ReferenceEvidenceRecoveryError(
                "Snapshot profile checksum does not match its protected checksum record."
            )

        profile_path = self.evidence.profile_builder.profile_path(profile_name)
        current_profile = self._protected_file(profile_path).read_bytes()
        self._profile_document(current_profile, profile_name, reference_id, "current")
        annotation_path = self.evidence.annotations.path(reference_id)
        if annotation_path.exists():
            self._protected_file(annotation_path)
        current_annotation = annotation_path.read_bytes() if annotation_path.exists() else None
        self.evidence.annotations.read(reference_id)

        annotation_root = snapshot / "reference_annotations"
        absent_marker = snapshot / "reference_annotations.absent"
        if absent_marker.exists() and annotation_root.exists():
            raise ReferenceEvidenceRecoveryError(
                "Snapshot annotation state is ambiguous."
            )
        target_annotation: bytes | None
        if absent_marker.exists():
            self._protected_file(absent_marker)
            target_annotation = None
        elif annotation_root.is_dir():
            self._protected_directory(annotation_root, require_private=False)
            target_path = annotation_root / f"{reference_id}.json"
            if target_path.exists():
                self._protected_file(target_path)
                ReferenceAnnotationStore(annotation_root).read(reference_id)
                target_annotation = target_path.read_bytes()
            else:
                target_annotation = None
        else:
            raise ReferenceEvidenceRecoveryError(
                "Snapshot does not declare whether annotations were present."
            )
        return {
            "current_profile": current_profile,
            "current_annotation": current_annotation,
            "target_profile": target_profile,
            "target_annotation": target_annotation,
        }

    def _protected_directory(self, path: Path, *, require_private: bool = True) -> Path:
        try:
            metadata = path.lstat()
        except OSError as error:
            raise ReferenceEvidenceRecoveryError(
                f"Protected snapshot directory is unavailable: {path}."
            ) from error
        if not path.is_dir() or path.is_symlink():
            raise ReferenceEvidenceRecoveryError(
                f"Protected snapshot directory is invalid: {path}."
            )
        if metadata.st_uid != self.owner_uid or (
            require_private and metadata.st_mode & 0o077
        ):
            raise ReferenceEvidenceRecoveryError(
                "Protected snapshot must be owned by the current user and inaccessible "
                "to group and other users."
            )
        return path

    def _protected_file(self, path: Path) -> Path:
        try:
            metadata = path.lstat()
        except OSError as error:
            raise ReferenceEvidenceRecoveryError(
                f"Required recovery file is unavailable: {path}."
            ) from error
        if not path.is_file() or path.is_symlink() or metadata.st_uid != self.owner_uid:
            raise ReferenceEvidenceRecoveryError(
                f"Required recovery file is unsafe: {path}."
            )
        # The protected snapshot root is validated separately. Internal
        # directories may retain the source application's normal permissions,
        # while individual files must remain owned and non-symlinked.
        return path

    @staticmethod
    def _profile_document(
        raw: bytes, profile_name: str, reference_id: str, label: str
    ) -> dict[str, Any]:
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ReferenceEvidenceRecoveryError(
                f"The {label} profile is not valid JSON."
            ) from error
        reference_ids = document.get("reference_ids") if isinstance(document, dict) else None
        if (
            not isinstance(document, dict)
            or document.get("version") not in {1, 2, 3}
            or document.get("profile_name") != profile_name
            or not isinstance(reference_ids, list)
            or not all(isinstance(value, str) for value in reference_ids)
            or len(reference_ids) != len(set(reference_ids))
            or reference_id not in reference_ids
            or (document.get("version") == 3 and document.get("category") != profile_name)
        ):
            raise ReferenceEvidenceRecoveryError(
                f"The {label} profile identity is invalid."
            )
        return document

    def _snapshot_profile_hash(self, snapshot: Path, profile_name: str) -> str:
        candidates: set[str] = set()
        for checksum_path in (
            snapshot / "checksums.sha256",
            snapshot / f"{profile_name}.sha256",
        ):
            if not checksum_path.exists():
                continue
            self._protected_file(checksum_path)
            for line in checksum_path.read_text(encoding="utf-8").splitlines():
                parts = line.strip().split(maxsplit=1)
                if len(parts) != 2 or not PROFILE_HASH_PATTERN.fullmatch(parts[0]):
                    continue
                recorded_path = parts[1].lstrip("*")
                if (
                    Path(recorded_path).name == f"{profile_name}.json"
                    and "reference_profiles" in Path(recorded_path).parts
                ):
                    candidates.add(parts[0])
        if len(candidates) != 1:
            raise ReferenceEvidenceRecoveryError(
                "Snapshot must contain one unambiguous profile checksum record."
            )
        return next(iter(candidates))

    def _failure_event(self, **values: Any) -> None:
        error = values.pop("error")
        try:
            self.evidence.audit.append(
                self.evidence.audit.event(
                    action="evidence_recovery",
                    result="failure",
                    reviewer=self.evidence.reviewer_name,
                    failure_reason=str(error),
                    **values,
                )
            )
        except ReferenceEvidenceAuditError:
            pass
