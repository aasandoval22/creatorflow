"""Transactional reference reanalysis, annotation, profile, and audit workflows."""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Mapping

from backend.services.reference_annotations import (
    DEFAULT_ANNOTATION_ROOT,
    ReferenceAnnotationError,
    ReferenceAnnotationStore,
)
from backend.services.reference_clip_analyzer import (
    ReferenceAnalysisError,
    ReferenceClipAnalyzer,
)
from backend.services.reference_clip_library import (
    ReferenceClipError,
    ReferenceClipLibrary,
)
from backend.services.reference_decision_audit import configured_reviewer_name
from backend.services.reference_evidence_audit import (
    ReferenceEvidenceAuditError,
    ReferenceEvidenceAuditLedger,
)
from backend.services.reference_profile_builder import (
    ReferenceProfileBuilder,
    ReferenceProfileError,
)


DEFAULT_EVIDENCE_AUDIT_PATH = DEFAULT_ANNOTATION_ROOT / "events.jsonl"
DEFAULT_EVIDENCE_LOCK_PATH = DEFAULT_ANNOTATION_ROOT / ".evidence.lock"
REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")


class ReferenceEvidenceError(RuntimeError):
    """An accepted-reference evidence operation failed safely."""


def analysis_revision(document: Mapping[str, Any] | None) -> int:
    if document is None:
        return 0
    value = document.get("analysis_revision", 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReferenceEvidenceError("Reference analysis revision is invalid.")
    return value


def _atomic_restore(path: Path, snapshot: bytes | None) -> None:
    if snapshot is None:
        path.unlink(missing_ok=True)
        return
    # Validate that rollback material is still JSON before replacing anything.
    try:
        json.loads(snapshot.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReferenceEvidenceError("Reference rollback snapshot is invalid.") from error
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(snapshot)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class ReferenceEvidenceService:
    """One guarded service layer for CLI and loopback review operations."""

    def __init__(
        self,
        library: ReferenceClipLibrary,
        *,
        annotations: ReferenceAnnotationStore | None = None,
        audit: ReferenceEvidenceAuditLedger | None = None,
        profile_builder: ReferenceProfileBuilder | None = None,
        analyzer_factory: Callable[[ReferenceClipLibrary], Any] = ReferenceClipAnalyzer,
        lock_path: Path = DEFAULT_EVIDENCE_LOCK_PATH,
        reviewer_name: str | None = None,
    ) -> None:
        self.library = library
        self.annotations = annotations or ReferenceAnnotationStore()
        self.audit = audit or ReferenceEvidenceAuditLedger(DEFAULT_EVIDENCE_AUDIT_PATH)
        self.profile_builder = profile_builder or ReferenceProfileBuilder(
            library, annotation_store=self.annotations
        )
        self.analyzer_factory = analyzer_factory
        self.lock_path = Path(lock_path)
        self.reviewer_name = configured_reviewer_name(reviewer_name)

    @contextmanager
    def locked(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as stream:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def accepted_entry(self, reference_id: str) -> dict[str, Any]:
        try:
            entry = self.library.get(reference_id)
        except ReferenceClipError as error:
            raise ReferenceEvidenceError(str(error)) from error
        if entry["status"] != "accepted":
            raise ReferenceEvidenceError(
                f"Reference {reference_id} is not accepted and cannot contribute evidence."
            )
        return entry

    def list_accepted(self, *, profile_name: str | None = None) -> list[dict[str, Any]]:
        return [
            self.inspect(entry["reference_id"])
            for entry in self.library.list_references(
                status="accepted", profile_name=profile_name
            )
        ]

    def inspect(self, reference_id: str) -> dict[str, Any]:
        entry = self.accepted_entry(reference_id)
        analysis = self._load_analysis(
            self.library.paths.resolve(
                entry["analysis_path"], must_exist=True, regular=True
            ),
            reference_id,
        )
        annotation = self.annotations.read(reference_id)
        baseline = self._load_json(
            self.library.paths.resolve(
                entry["baseline_path"], must_exist=True, regular=True
            ),
            "baseline",
        )
        profile_membership = []
        if self.profile_builder.output_directory.exists():
            for path in sorted(self.profile_builder.output_directory.glob("*.json")):
                try:
                    profile = self.profile_builder.read(path.stem)
                except (ReferenceProfileError, OSError):
                    continue
                if reference_id in profile.get("reference_ids", []):
                    profile_membership.append({
                        "profile_name": path.stem,
                        "staleness": profile.get("staleness", {"status": "unavailable"}),
                    })
        checksum_valid = True
        checksum_error = None
        try:
            self.library.validate_checksum(reference_id)
        except ReferenceClipError as error:
            checksum_valid = False
            checksum_error = str(error)
        source_video_id = baseline.get("source_video_id")
        source_url = (
            f"https://www.youtube.com/watch?v={source_video_id}"
            if isinstance(source_video_id, str) and source_video_id
            else None
        )
        return {
            "entry": entry,
            "analysis": analysis,
            "annotation": annotation,
            "annotation_exists": self.annotations.exists(reference_id),
            "baseline": {
                "qualities": baseline.get("qualities", []),
                "notes": baseline.get("notes", ""),
                "source_title": baseline.get("source_title", ""),
            },
            "source_url": source_url,
            "profile_membership": profile_membership,
            "checksum_valid": checksum_valid,
            "checksum_error": checksum_error,
            "history": self.audit.history(reference_id=reference_id, limit=10),
        }

    def update_annotations(
        self,
        reference_id: str,
        *,
        expected_revision: int,
        values: Mapping[str, Any],
        request_id: str | None = None,
    ) -> dict[str, Any]:
        with self.locked():
            self._require_unused_request(request_id)
            entry = self.accepted_entry(reference_id)
            before = self.annotations.read(reference_id)
            snapshot = self.annotations.snapshot(reference_id)
            profile_snapshots: dict[Path, bytes] = {}
            try:
                updated, changed = self.annotations.update(
                    reference_id,
                    expected_revision=expected_revision,
                    values=values,
                    reviewer=self.reviewer_name,
                )
                profile_snapshots = self.profile_builder.mark_stale(
                    reference_id,
                    reason=f"{reference_id} annotation revision changed.",
                )
                self.audit.append(self.audit.event(
                    action="annotation_update",
                    result="success",
                    reference_id=reference_id,
                    profile_name=entry["profile_name"],
                    previous_annotation_revision=before["revision"],
                    new_annotation_revision=updated["revision"],
                    changed_fields=changed,
                    reviewer=self.reviewer_name,
                    request_id=request_id,
                ))
                return updated
            except (ReferenceAnnotationError, ReferenceEvidenceAuditError) as error:
                self.annotations.restore(reference_id, snapshot)
                self._restore_profiles(profile_snapshots)
                self._append_failure(
                    action="annotation_update",
                    reference_id=reference_id,
                    profile_name=entry["profile_name"],
                    previous_annotation_revision=before["revision"],
                    new_annotation_revision=before["revision"],
                    request_id=request_id,
                    error=(
                        ReferenceAnnotationError("Stale annotation revision.")
                        if "Stale annotation form" in str(error)
                        else ReferenceAnnotationError(
                            "Annotation validation or persistence failed."
                        )
                    ),
                )
                raise
            except Exception as error:
                self.annotations.restore(reference_id, snapshot)
                self._restore_profiles(profile_snapshots)
                self._append_failure(
                    action="annotation_update",
                    reference_id=reference_id,
                    profile_name=entry["profile_name"],
                    previous_annotation_revision=before["revision"],
                    new_annotation_revision=before["revision"],
                    request_id=request_id,
                    error=ReferenceEvidenceError(
                        "Annotation persistence or profile staleness update failed."
                    ),
                )
                raise ReferenceEvidenceError(
                    f"Annotation update failed; previous annotations were restored: {error}"
                ) from error

    def reanalyze(
        self,
        reference_id: str,
        *,
        transcription: bool,
        force: bool,
        expected_annotation_revision: int | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        with self.locked():
            self._require_unused_request(request_id)
            entry = self.accepted_entry(reference_id)
            self._require_annotation_revision(
                reference_id, expected_annotation_revision
            )
            path = self.library.paths.resolve(entry["analysis_path"])
            snapshot = path.read_bytes() if path.exists() else None
            previous = self._load_analysis(path, reference_id) if snapshot is not None else None
            before_revision = analysis_revision(previous)
            profile_snapshots: dict[Path, bytes] = {}
            try:
                document = self.analyzer_factory(self.library).analyze(
                    reference_id, transcription=transcription, force=force
                )
            except Exception as error:
                _atomic_restore(path, snapshot)
                self._append_failure(
                    action="reanalyze", reference_id=reference_id,
                    profile_name=entry["profile_name"],
                    previous_analysis_revision=before_revision,
                    new_analysis_revision=before_revision,
                    request_id=request_id, error=error,
                )
                if isinstance(error, (ReferenceAnalysisError, ReferenceEvidenceError)):
                    raise
                raise ReferenceEvidenceError(
                    f"Reanalysis failed; previous analysis was preserved: {error}"
                ) from error
            try:
                new_revision = analysis_revision(document)
                if new_revision != before_revision:
                    profile_snapshots = self.profile_builder.mark_stale(
                        reference_id,
                        reason=f"{reference_id} analysis revision changed.",
                    )
                self.audit.append(self.audit.event(
                    action="reanalyze", result="success",
                    reference_id=reference_id,
                    profile_name=entry["profile_name"],
                    previous_analysis_revision=before_revision,
                    new_analysis_revision=new_revision,
                    reviewer=self.reviewer_name,
                    request_id=request_id,
                ))
            except Exception as error:
                _atomic_restore(path, snapshot)
                self._restore_profiles(profile_snapshots)
                if isinstance(error, (ReferenceEvidenceAuditError, ReferenceProfileError)):
                    raise
                raise ReferenceEvidenceError(
                    "Reanalysis bookkeeping failed; previous analysis and profile "
                    f"staleness were restored: {error}"
                ) from error
            return document

    def rebuild_profile(
        self, profile_name: str, *, request_id: str | None = None,
        trigger_reference_id: str | None = None,
        expected_annotation_revision: int | None = None,
    ) -> dict[str, Any]:
        with self.locked():
            self._require_unused_request(request_id)
            if trigger_reference_id is not None:
                entry = self.accepted_entry(trigger_reference_id)
                if entry["profile_name"] != profile_name:
                    raise ReferenceEvidenceError(
                        "Reference category does not match the requested profile."
                    )
                self._require_annotation_revision(
                    trigger_reference_id, expected_annotation_revision
                )
            path = self.profile_builder.profile_path(profile_name)
            snapshot = path.read_bytes() if path.exists() else None
            try:
                profile = self.profile_builder.build(profile_name)
            except Exception as error:
                _atomic_restore(path, snapshot)
                self._append_failure(
                    action="profile_rebuild", profile_name=profile_name,
                    request_id=request_id, error=error,
                )
                if isinstance(error, ReferenceProfileError):
                    raise
                raise ReferenceEvidenceError(
                    f"Profile rebuild failed; previous profile was preserved: {error}"
                ) from error
            try:
                self.audit.append(self.audit.event(
                    action="profile_rebuild", result="success",
                    profile_name=profile_name,
                    reference_ids=profile["reference_ids"],
                    reviewer=self.reviewer_name,
                    request_id=request_id,
                ))
            except ReferenceEvidenceAuditError:
                _atomic_restore(path, snapshot)
                raise
            return profile

    def history(
        self, *, reference_id: str | None = None,
        profile_name: str | None = None, limit: int | None = None,
    ) -> list[dict[str, Any]]:
        return self.audit.history(
            reference_id=reference_id, profile_name=profile_name, limit=limit
        )

    def _append_failure(self, *, action: str, error: Exception, **values: Any) -> None:
        try:
            self.audit.append(self.audit.event(
                action=action, result="failure", reviewer=self.reviewer_name,
                failure_reason=str(error), **values,
            ))
        except ReferenceEvidenceAuditError:
            pass

    @staticmethod
    def _restore_profiles(snapshots: Mapping[Path, bytes]) -> None:
        for path, snapshot in snapshots.items():
            _atomic_restore(path, snapshot)

    def _require_unused_request(self, request_id: str | None) -> None:
        if request_id is None:
            return
        if not REQUEST_ID_PATTERN.fullmatch(request_id):
            raise ReferenceEvidenceError("Evidence request ID is invalid; refresh and retry.")
        if any(event.get("request_id") == request_id for event in self.audit.history()):
            raise ReferenceEvidenceError(
                "This evidence request was already processed; refresh before retrying."
            )

    def _require_annotation_revision(
        self, reference_id: str, expected_revision: int | None
    ) -> None:
        if expected_revision is None:
            return
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise ReferenceEvidenceError("Expected annotation revision is invalid.")
        current = self.annotations.read(reference_id)["revision"]
        if current != expected_revision:
            raise ReferenceEvidenceError(
                f"Stale annotation form: expected revision {expected_revision}, "
                f"current revision is {current}. Refresh and retry."
            )

    @staticmethod
    def _load_json(path: Path, label: str) -> dict[str, Any]:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ReferenceEvidenceError(f"Cannot read reference {label} {path}: {error}.") from error
        if not isinstance(document, dict):
            raise ReferenceEvidenceError(f"Reference {label} {path} is malformed.")
        return document

    @classmethod
    def _load_analysis(
        cls, path: Path, reference_id: str
    ) -> dict[str, Any] | None:
        if not path.exists():
            return None
        document = cls._load_json(path, "analysis")
        if document.get("reference_id") != reference_id or document.get("version") not in {1, 2}:
            raise ReferenceEvidenceError(f"Reference analysis {path} is malformed.")
        analysis_revision(document)
        return document
