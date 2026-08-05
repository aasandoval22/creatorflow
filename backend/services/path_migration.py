"""Audited planning, recovery, and ownership coverage for persistent paths."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from backend.services.persistent_paths import (
    DEFAULT_LEGACY_DATA_ROOTS,
    DEFAULT_PERSISTENT_DATA_ROOT,
    PersistentPathError,
    PersistentPathResolver,
)
from backend.services.publication import safe_publication_text, sha256_file
from backend.services.video_manifest import utc_now


MIGRATION_VERSION = 1
PLAN_ID = re.compile(r"pathplan_[0-9a-f]{24}")
RECOVERY_ID = re.compile(r"pathrecovery_[0-9a-f]{24}")
MEDIA_SUFFIXES = frozenset({".mp4", ".webm", ".mkv", ".mov", ".m4a", ".mp3", ".wav"})
PATH_FIELD_NAMES = frozenset({
    "local_file_path", "transcript_json_path", "transcript_text_path",
    "subtitle_srt_path", "candidates_json_path", "preview_path",
    "preview_metadata_path", "rendered_media_path", "media_path",
    "source_info_path", "baseline_path", "analysis_path",
    "source_media_path", "source_transcript_path", "source_candidates_path",
    "output_path", "metadata_path", "report_path", "profile_path",
})
URL_FIELD_NAMES = frozenset({"url", "video_url", "channel_url", "source_url", "share_url"})


class PathMigrationError(ValueError):
    """A path migration cannot proceed without operator action."""


def _json_bytes(document: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _atomic_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(_json_bytes(document).decode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _document_hash(value: Mapping[str, Any]) -> str:
    candidate = {key: copy.deepcopy(item) for key, item in value.items()
                 if key != "content_sha256"}
    raw = json.dumps(candidate, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _file_hash(path: Path) -> str:
    return sha256_file(path)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise PathMigrationError(f"Stored JSON {path} is corrupt: {error}.") from error
    except OSError as error:
        raise PathMigrationError(f"Cannot read stored JSON {path}: {error}.") from error


def _read_inventory_documents(path: Path) -> list[tuple[str | None, Any]]:
    """Read JSON metadata or immutable JSONL audit entries for classification."""

    if path.suffix != ".jsonl":
        return [(None, _read_json(path))]
    documents = []
    try:
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    documents.append((f"line:{line_number}", json.loads(line)))
                except json.JSONDecodeError as error:
                    raise PathMigrationError(
                        f"Stored JSONL {path} line {line_number} is corrupt: {error}."
                    ) from error
    except OSError as error:
        raise PathMigrationError(f"Cannot read stored metadata {path}: {error}.") from error
    return documents


def _pointer_get(value: Any, pointer: Sequence[str | int]) -> Any:
    current = value
    for part in pointer:
        current = current[part]
    return current


def _pointer_set(value: Any, pointer: Sequence[str | int], replacement: Any) -> None:
    current = value
    for part in pointer[:-1]:
        current = current[part]
    current[pointer[-1]] = replacement


def _walk(value: Any, pointer: tuple[str | int, ...] = ()) -> Iterator[tuple[tuple[str | int, ...], str, str]]:
    if isinstance(value, dict):
        for key, child in value.items():
            child_pointer = pointer + (key,)
            if isinstance(child, str) and (
                key in PATH_FIELD_NAMES or key.endswith("_path")
                or key in URL_FIELD_NAMES or key.endswith("_url")
            ):
                yield child_pointer, key, child
            else:
                yield from _walk(child, child_pointer)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, pointer + (index,))


def _contains_scalar(value: Any, wanted: str) -> bool:
    if value == wanted:
        return True
    if isinstance(value, dict):
        return any(_contains_scalar(child, wanted) for child in value.values())
    if isinstance(value, list):
        return any(_contains_scalar(child, wanted) for child in value)
    return False


def _record_identity(document: Any, pointer: Sequence[str | int]) -> str:
    current = document
    identity = None
    for part in pointer[:-1]:
        current = current[part]
        if isinstance(current, dict):
            for key in (
                "review_id", "video_id", "reference_id", "attempt_id",
                "candidate_id", "batch_id", "recovery_id",
            ):
                if isinstance(current.get(key), str):
                    identity = f"{key}={current[key]}"
                    break
    return identity or "document"


def _schema_for(path: Path, data_root: Path) -> tuple[str, bool]:
    relative = path.relative_to(data_root).as_posix()
    active = True
    if relative == "manifests/videos.json":
        return "source_manifest", active
    if relative == "review_queue/reviews.json":
        return "review_queue", active
    if relative == "publication/records.json":
        return "publication_state", active
    if relative == "reference_clips/index.json":
        return "reference_index", active
    if relative == "reference_discovery/candidates.json":
        return "reference_discovery_queue", active
    if relative.startswith("transcripts/") and path.name == "transcript.json":
        return "transcript_artifact", active
    if relative.startswith("clip_candidates/") and path.name == "candidates.json":
        return "candidate_artifact", active
    if relative.startswith("previews/") and path.name == "preview.json":
        return "preview_metadata", active
    if relative.startswith("reference_comparisons/"):
        return "comparison_report", active
    if relative.startswith("review_comparison_batches/"):
        return "comparison_batch", active
    if relative.startswith("media_cleanup/plans/"):
        return "immutable_cleanup_plan", False
    if "recovery" in Path(relative).parts:
        return "immutable_recovery_record", False
    if path.suffix == ".jsonl":
        return "immutable_audit_history", False
    return "stored_metadata", False


def _active_json_files(data_root: Path) -> list[Path]:
    patterns = (
        "manifests/videos.json", "review_queue/reviews.json",
        "publication/records.json", "reference_clips/index.json",
        "reference_discovery/candidates.json", "transcripts/*/transcript.json",
        "clip_candidates/*/candidates.json", "previews/**/preview.json",
        "reference_comparisons/**/*.json", "review_comparison_batches/**/*.json",
    )
    result: set[Path] = set()
    for pattern in patterns:
        result.update(path for path in data_root.glob(pattern) if path.is_file())
    return sorted(result)


def _immutable_json_files(data_root: Path) -> list[Path]:
    roots = (
        data_root / "reference_evidence_recovery",
        data_root / "reference_discovery" / "withdrawal_recovery",
        data_root / "media_cleanup" / "plans",
        data_root / "path_migration" / "recovery",
    )
    result: set[Path] = set()
    for root in roots:
        if root.is_dir():
            result.update(root.rglob("*.json"))
            result.update(root.rglob("*.jsonl"))
    # JSONL files are append-only audit or operational history. They are
    # inventoried for historical path text but never offered for mutation.
    result.update(data_root.rglob("*.jsonl"))
    return sorted(path for path in result if path.is_file())


def load_orphan_registry(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "updated_at": utc_now(), "entries": []}
    value = _read_json(path)
    if (
        not isinstance(value, dict) or set(value) != {"version", "updated_at", "entries"}
        or value["version"] != 1 or not isinstance(value["entries"], list)
    ):
        raise PathMigrationError("Orphan ownership registry is malformed.")
    fields = {
        "media_path", "video_id", "creator", "size_bytes", "checksum_sha256",
        "evidence_paths", "adopted_at", "adoption_source",
    }
    identities = set()
    for entry in value["entries"]:
        if not isinstance(entry, dict) or set(entry) != fields:
            raise PathMigrationError("Orphan ownership registry entry is malformed.")
        for field in ("media_path", "video_id", "creator", "adopted_at", "adoption_source"):
            if not isinstance(entry[field], str) or not entry[field].strip():
                raise PathMigrationError(f"Orphan registry {field} is malformed.")
        relative = PurePosixPath(entry["media_path"])
        if (
            relative.is_absolute() or "\\" in entry["media_path"]
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise PathMigrationError("Orphan registry media path is unsafe.")
        if (
            isinstance(entry["size_bytes"], bool)
            or not isinstance(entry["size_bytes"], int)
            or entry["size_bytes"] < 0
            or not isinstance(entry["checksum_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", entry["checksum_sha256"])
            or not isinstance(entry["evidence_paths"], list)
            or any(not isinstance(item, str) or not item for item in entry["evidence_paths"])
        ):
            raise PathMigrationError("Orphan registry evidence or media identity is malformed.")
        if entry["media_path"] in identities:
            raise PathMigrationError("Orphan registry contains duplicate media ownership.")
        identities.add(entry["media_path"])
    return value


def _registry_entry(
    association: Mapping[str, Any], *, adopted_at: str, source: str,
) -> dict[str, Any]:
    return {
        "media_path": association["media_path"],
        "video_id": association["video_id"],
        "creator": association["creator"],
        "size_bytes": association["size_bytes"],
        "checksum_sha256": association["checksum_sha256"],
        "evidence_paths": sorted(set(association["evidence_paths"])),
        "adopted_at": adopted_at,
        "adoption_source": source,
    }


class PathMigrationService:
    """Plan and atomically normalize active path records without moving media."""

    def __init__(
        self, data_root: Path = DEFAULT_PERSISTENT_DATA_ROOT, *,
        legacy_roots: Iterable[Path] = DEFAULT_LEGACY_DATA_ROOTS,
        migration_root: Path | None = None,
        production_root: Path | None = None,
        owner_uid: int | None = None,
    ) -> None:
        self.data_root = Path(data_root).resolve()
        self.paths = PersistentPathResolver(
            self.data_root, legacy_roots=legacy_roots,
            production_root=production_root, owner_uid=owner_uid,
        )
        self.migration_root = Path(migration_root or self.data_root / "path_migration")
        self.plan_root = self.migration_root / "plans"
        self.recovery_root = self.migration_root / "recovery"
        self.audit_path = self.migration_root / "events.jsonl"
        self.registry_path = self.migration_root / "orphan_ownership.json"
        self.lock_path = self.migration_root / "path_migration.lock"
        self.owner_uid = self.paths.owner_uid

    def inventory(self) -> dict[str, Any]:
        entries = []
        files = _active_json_files(self.data_root)
        immutable = _immutable_json_files(self.data_root)
        for path in files + immutable:
            schema, mutable = _schema_for(path, self.data_root)
            try:
                documents = _read_inventory_documents(path)
            except PathMigrationError as error:
                entries.append({
                    "document_path": path.relative_to(self.data_root).as_posix(),
                    "schema": schema, "field_name": None, "pointer": [],
                    "value": None, "classification": "unsafe_or_unrecognized",
                    "mutable": mutable, "reason": str(error),
                })
                continue
            for line_identity, document in documents:
                for pointer, field, value in _walk(document):
                    if field in URL_FIELD_NAMES or field.endswith("_url"):
                        category = "external_url"
                        relative = None
                        reason = "URL is not a managed filesystem path"
                    elif not mutable:
                        category = "intentionally_immutable_historical_text"
                        relative = None
                        reason = "historical audit or recovery content is immutable"
                    else:
                        classification = self.paths.classify(value)
                        category = classification.category
                        relative = classification.relative_path
                        reason = classification.reason
                    entries.append({
                        "document_path": path.relative_to(self.data_root).as_posix(),
                        "schema": schema, "field_name": field,
                        "pointer": list(pointer), "value": value,
                        "classification": category, "canonical_relative": relative,
                        "mutable": mutable, "reason": reason,
                        "owning_record": line_identity
                        or _record_identity(document, pointer),
                    })
        counts = Counter(entry["classification"] for entry in entries)
        return {
            "version": 1, "created_at": utc_now(),
            "data_root": str(self.data_root), "entries": entries,
            "classification_counts": dict(sorted(counts.items())),
            "download_archive": {
                "path": "database/downloaded_videos.txt",
                "classification": "canonical immutable identifier with derived path",
            },
            "processing_state": {
                "path": "production/processing_state.json",
                "classification": "canonical immutable video identifiers; no active paths",
            },
        }

    def plan(self) -> dict[str, Any]:
        inventory = self.inventory()
        source_documents: dict[str, dict[str, Any]] = {}
        changes = []
        manual = []
        active_by_path = {
            path.relative_to(self.data_root).as_posix(): path
            for path in _active_json_files(self.data_root)
        }
        for entry in inventory["entries"]:
            if not entry.get("mutable") or entry.get("field_name") is None:
                continue
            category = entry["classification"]
            if category == "canonical_persistent_relative" or category == "external_url":
                continue
            if category not in {
                "legacy_development_absolute", "legacy_persistent_absolute",
                "production_release_absolute",
            }:
                manual.append({**entry, "manual_reason": entry["reason"]})
                continue
            document_path = active_by_path[entry["document_path"]]
            source = source_documents.setdefault(entry["document_path"], {
                "document_path": entry["document_path"],
                "source_sha256": _file_hash(document_path),
                "size_bytes": document_path.stat().st_size,
            })
            try:
                target, info = self.paths.validate_migration_target(entry["value"])
                checksum = _file_hash(target)
            except (OSError, PersistentPathError) as error:
                manual.append({
                    **entry, "manual_reason": safe_publication_text(error),
                })
                continue
            changes.append({
                "document_path": entry["document_path"],
                "source_sha256": source["source_sha256"],
                "schema": entry["schema"],
                "owning_record": entry["owning_record"],
                "field_name": entry["field_name"],
                "pointer": entry["pointer"],
                "old_value": entry["value"],
                "proposed_value": entry["canonical_relative"],
                "target_identity": {"device": info.st_dev, "inode": info.st_ino},
                "size_bytes": info.st_size, "checksum_sha256": checksum,
                "owner_uid": info.st_uid,
                "safety": {
                    "regular_file": stat.S_ISREG(info.st_mode),
                    "expected_owner": info.st_uid == self.owner_uid,
                    "canonical_root_join": True, "symlink_traversal": False,
                },
                "reason": "recognized legacy path maps to the same verified canonical file",
            })
        orphan = self._orphan_analysis()
        plan_id = f"pathplan_{uuid.uuid4().hex[:24]}"
        projected = build_media_coverage(
            self.data_root, resolver=self.paths,
            projected_adoptions=orphan["proven_associations"],
        )
        document = {
            "version": MIGRATION_VERSION, "plan_id": plan_id,
            "created_at": utc_now(), "data_root": str(self.data_root),
            "recognized_legacy_roots": [str(root) for root in self.paths.legacy_roots],
            "recognized_production_root": str(self.paths.production_root),
            "source_documents": sorted(source_documents.values(), key=lambda x: x["document_path"]),
            "changes": sorted(changes, key=lambda x: (x["document_path"], str(x["pointer"]))),
            "manual_review": sorted(manual, key=lambda x: (x["document_path"], str(x["pointer"]))),
            "inventory_summary": inventory["classification_counts"],
            "orphan_analysis": orphan,
            "coverage_before": build_media_coverage(self.data_root, resolver=self.paths),
            "coverage_projected": projected,
        }
        document["content_sha256"] = _document_hash(document)
        path = self.plan_path(plan_id)
        if path.exists():
            raise PathMigrationError("Path migration plan identity unexpectedly exists.")
        _atomic_json(path, document)
        self._audit("plan_created", plan_id=plan_id, changes=len(changes),
                    manual_review=len(manual))
        return document

    def show(self, plan_id: str) -> dict[str, Any]:
        path = self.plan_path(plan_id)
        value = _read_json(path)
        if (
            not isinstance(value, dict) or value.get("version") != MIGRATION_VERSION
            or value.get("plan_id") != plan_id
            or value.get("data_root") != str(self.data_root)
            or value.get("content_sha256") != _document_hash(value)
        ):
            raise PathMigrationError("Path migration plan identity or checksum is invalid.")
        return value

    def plan_path(self, plan_id: str) -> Path:
        if not isinstance(plan_id, str) or not PLAN_ID.fullmatch(plan_id):
            raise PathMigrationError("Path migration plan ID is invalid.")
        return self.plan_root / f"{plan_id}.json"

    def apply(self, plan_id: str, *, confirm: str) -> dict[str, Any]:
        if confirm != plan_id:
            raise PathMigrationError("Migration confirmation does not match plan ID.")
        plan = self.show(plan_id)
        existing = self._recovery_for_plan(plan_id)
        if existing and existing.get("state") == "completed":
            return existing
        with self._all_locks():
            existing = self._recovery_for_plan(plan_id)
            if existing and existing.get("state") == "completed":
                return existing
            recovery_id = (
                existing["recovery_id"] if existing
                else f"pathrecovery_{uuid.uuid4().hex[:24]}"
            )
            directory = self.recovery_root / recovery_id
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for change in plan["changes"]:
                groups[change["document_path"]].append(change)
            manifest = existing or {
                "version": 1, "recovery_id": recovery_id, "plan_id": plan_id,
                "plan_sha256": plan["content_sha256"], "created_at": utc_now(),
                "updated_at": utc_now(), "state": "applying", "documents": [],
                "orphan_registry": None,
            }
            _atomic_json(directory / "manifest.json", manifest)
            changed_prepared = False
            for item in list(manifest["documents"]):
                if item.get("status") != "prepared":
                    continue
                path = self.paths.resolve(
                    item["document_path"], must_exist=True, regular=True
                )
                current = _file_hash(path)
                if current == item["after_sha256"]:
                    item["status"] = "migrated"
                    changed_prepared = True
                elif current == item["before_sha256"]:
                    manifest["documents"].remove(item)
                    changed_prepared = True
                else:
                    raise PathMigrationError(
                        f"Interrupted migration record {item['document_path']} "
                        "changed unexpectedly; restore or inspect it before retrying."
                    )
            if changed_prepared:
                manifest["updated_at"] = utc_now()
                _atomic_json(directory / "manifest.json", manifest)
            orphan_state = manifest.get("orphan_registry")
            if orphan_state and orphan_state.get("status") == "prepared":
                exists = self.registry_path.exists()
                current = _file_hash(self.registry_path) if exists else None
                before_matches = (
                    orphan_state.get("before_existed") and exists
                    and current == orphan_state.get("before_sha256")
                ) or (not orphan_state.get("before_existed") and not exists)
                if current == orphan_state.get("after_sha256"):
                    orphan_state["status"] = "completed"
                elif before_matches:
                    manifest["orphan_registry"] = None
                else:
                    raise PathMigrationError(
                        "Interrupted orphan registry changed unexpectedly; "
                        "restore or inspect it before retrying."
                    )
                manifest["updated_at"] = utc_now()
                _atomic_json(directory / "manifest.json", manifest)
            completed = {
                item["document_path"] for item in manifest["documents"]
                if item.get("status") in {"migrated", "skipped"}
            }
            for relative, fields in sorted(groups.items()):
                if relative in completed:
                    continue
                prepared_index: int | None = None

                def record_prepared(record: dict[str, Any]) -> None:
                    nonlocal prepared_index
                    manifest["documents"].append(record)
                    prepared_index = len(manifest["documents"]) - 1
                    manifest["updated_at"] = utc_now()
                    _atomic_json(directory / "manifest.json", manifest)

                result = self._apply_document(
                    directory, relative, fields,
                    prepared_callback=record_prepared,
                )
                if prepared_index is None:
                    manifest["documents"].append(result)
                else:
                    manifest["documents"][prepared_index] = result
                manifest["updated_at"] = utc_now()
                _atomic_json(directory / "manifest.json", manifest)
                self._audit("document_" + result["status"], plan_id=plan_id,
                            recovery_id=recovery_id, document_path=relative,
                            detail=result.get("detail"))
            if manifest["orphan_registry"] is None:
                def record_orphan_prepared(record: dict[str, Any]) -> None:
                    manifest["orphan_registry"] = record
                    manifest["updated_at"] = utc_now()
                    _atomic_json(directory / "manifest.json", manifest)

                manifest["orphan_registry"] = self._apply_associations(
                    directory, plan["orphan_analysis"]["proven_associations"],
                    prepared_callback=record_orphan_prepared,
                )
                _atomic_json(directory / "manifest.json", manifest)
            manifest["state"] = "completed"
            manifest["updated_at"] = utc_now()
            _atomic_json(directory / "manifest.json", manifest)
            self._audit("migration_completed", plan_id=plan_id,
                        recovery_id=recovery_id)
            return copy.deepcopy(manifest)

    def _apply_document(
        self, directory: Path, relative: str, changes: list[dict[str, Any]], *,
        prepared_callback: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        path = self.paths.resolve(relative, must_exist=True, regular=True)
        expected = changes[0]["source_sha256"]
        if _file_hash(path) != expected:
            return {"document_path": relative, "status": "skipped",
                    "detail": "Source-record hash changed after planning."}
        document = _read_json(path)
        candidate = copy.deepcopy(document)
        try:
            for change in changes:
                if _pointer_get(candidate, change["pointer"]) != change["old_value"]:
                    raise PathMigrationError("Stored field changed after planning.")
                target, info = self.paths.validate_migration_target(
                    change["old_value"], checksum=change["checksum_sha256"]
                )
                if (info.st_dev, info.st_ino) != (
                    change["target_identity"]["device"],
                    change["target_identity"]["inode"],
                ):
                    raise PathMigrationError("Target identity changed after planning.")
                _pointer_set(candidate, change["pointer"], change["proposed_value"])
        except (KeyError, IndexError, OSError, PersistentPathError, PathMigrationError) as error:
            return {"document_path": relative, "status": "skipped",
                    "detail": safe_publication_text(error)}
        backup = directory / "files" / relative
        backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(path, backup)
        os.chmod(backup, 0o600)
        before = _file_hash(backup)
        after = hashlib.sha256(_json_bytes(candidate)).hexdigest()
        prepared = {
            "document_path": relative, "status": "prepared", "detail": None,
            "backup_path": backup.relative_to(directory).as_posix(),
            "before_sha256": before, "after_sha256": after,
            "field_count": len(changes),
        }
        prepared_callback(copy.deepcopy(prepared))
        _atomic_json(path, candidate)
        if _file_hash(path) != after:
            raise PathMigrationError("Atomic migration output checksum is unexpected.")
        prepared["status"] = "migrated"
        return prepared

    def _apply_associations(
        self, directory: Path, associations: list[dict[str, Any]], *,
        prepared_callback: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        before_exists = self.registry_path.exists()
        before = self.registry_path.read_bytes() if before_exists else None
        registry = load_orphan_registry(self.registry_path)
        existing = {entry["media_path"]: entry for entry in registry["entries"]}
        added = 0
        skipped = []
        for association in associations:
            try:
                self._revalidate_association(association)
            except (OSError, PersistentPathError, PathMigrationError) as error:
                skipped.append({
                    "media_path": association.get("media_path"),
                    "video_id": association.get("video_id"),
                    "reason": safe_publication_text(error),
                })
                continue
            current = existing.get(association["media_path"])
            if current:
                if current["video_id"] != association["video_id"]:
                    skipped.append({
                        "media_path": association["media_path"],
                        "video_id": association["video_id"],
                        "reason": "Orphan ownership conflicts with existing registry.",
                    })
                continue
            entry = _registry_entry(
                association, adopted_at=utc_now(),
                source="audited_path_migration",
            )
            registry["entries"].append(entry)
            existing[entry["media_path"]] = entry
            added += 1
        registry["entries"].sort(key=lambda item: item["media_path"])
        registry["updated_at"] = utc_now()
        backup_path = directory / "orphan_ownership.before.json"
        if before is not None:
            backup_path.write_bytes(before)
            os.chmod(backup_path, 0o600)
        after_sha256 = (
            hashlib.sha256(_json_bytes(registry)).hexdigest() if added else None
        )
        result = {
            "status": "prepared" if added else "completed",
            "before_existed": before_exists,
            "before_sha256": hashlib.sha256(before).hexdigest()
            if before is not None else None,
            "backup_path": backup_path.relative_to(directory).as_posix()
            if before_exists else None,
            "after_sha256": after_sha256,
            "added": added, "skipped": skipped,
        }
        if added:
            prepared_callback(copy.deepcopy(result))
            _atomic_json(self.registry_path, registry)
            if _file_hash(self.registry_path) != after_sha256:
                raise PathMigrationError("Atomic orphan registry checksum is unexpected.")
            result["status"] = "completed"
        return result

    def _revalidate_association(self, association: Mapping[str, Any]) -> None:
        media = self.paths.resolve(
            association["media_path"], must_exist=True, regular=True
        )
        info = media.stat()
        expected_identity = association.get("target_identity") or {}
        if (
            info.st_dev != expected_identity.get("device")
            or info.st_ino != expected_identity.get("inode")
        ):
            raise PathMigrationError("Orphan media identity changed after planning.")
        if info.st_uid != association.get("owner_uid"):
            raise PathMigrationError("Orphan media ownership changed after planning.")
        if _file_hash(media) != association.get("checksum_sha256"):
            raise PathMigrationError("Orphan media checksum changed after planning.")
        video_id = association.get("video_id")
        evidence_ids = set()
        media_identity = (info.st_dev, info.st_ino)
        for evidence in association.get("evidence_paths", []):
            path = self.paths.resolve(evidence, must_exist=True, regular=True)
            value = _read_json(path)
            evidence_media = value.get("source_media_path") if isinstance(value, dict) else None
            evidence_video_id = value.get("video_id") if isinstance(value, dict) else None
            if not isinstance(evidence_media, str) or not isinstance(evidence_video_id, str):
                raise PathMigrationError("Orphan evidence lost media or video identity.")
            target = self.paths.resolve(evidence_media, must_exist=True, regular=True)
            target_info = target.stat()
            if (target_info.st_dev, target_info.st_ino) != media_identity:
                raise PathMigrationError("Orphan evidence points to different media.")
            evidence_ids.add(evidence_video_id)
        if evidence_ids != {video_id}:
            raise PathMigrationError("Orphan evidence no longer proves one video identity.")
        manifest_path = self.data_root / "manifests" / "videos.json"
        manifest = _read_json(manifest_path)
        matches = [
            record for record in manifest.get("videos", [])
            if record.get("video_id") == video_id
        ]
        if len(matches) != 1:
            raise PathMigrationError("Manifest no longer proves one orphan video identity.")

    def restore(self, recovery_id: str) -> dict[str, Any]:
        if not isinstance(recovery_id, str) or not RECOVERY_ID.fullmatch(recovery_id):
            raise PathMigrationError("Path migration recovery ID is invalid.")
        directory = self.recovery_root / recovery_id
        with self._all_locks():
            manifest = _read_json(directory / "manifest.json")
            if manifest.get("recovery_id") != recovery_id:
                raise PathMigrationError("Recovery manifest identity is invalid.")
            if manifest.get("state") == "restored":
                return manifest
            for record in reversed(manifest.get("documents", [])):
                if record.get("status") not in {"migrated", "prepared"}:
                    continue
                path = self.paths.resolve(record["document_path"], must_exist=True, regular=True)
                current = _file_hash(path)
                if record.get("status") == "prepared" and current == record["before_sha256"]:
                    record["status"] = "restored"
                    continue
                if current != record["after_sha256"]:
                    raise PathMigrationError(
                        f"Refusing to overwrite changed record {record['document_path']}."
                    )
                backup = directory / record["backup_path"]
                if _file_hash(backup) != record["before_sha256"]:
                    raise PathMigrationError("Migration recovery backup checksum changed.")
                os.replace(backup, path)
                record["status"] = "restored"
            orphan = manifest.get("orphan_registry") or {}
            if orphan.get("after_sha256"):
                exists = self.registry_path.exists()
                current = _file_hash(self.registry_path) if exists else None
                before_matches = (
                    orphan.get("before_existed") and exists
                    and current == orphan.get("before_sha256")
                ) or (not orphan.get("before_existed") and not exists)
                if orphan.get("status") == "prepared" and before_matches:
                    orphan["status"] = "restored"
                elif current != orphan["after_sha256"]:
                    raise PathMigrationError("Orphan registry changed after migration.")
                else:
                    if orphan.get("before_existed"):
                        os.replace(directory / orphan["backup_path"], self.registry_path)
                    else:
                        self.registry_path.unlink(missing_ok=True)
                    orphan["status"] = "restored"
            manifest["state"] = "restored"
            manifest["updated_at"] = utc_now()
            _atomic_json(directory / "manifest.json", manifest)
            self._audit("migration_restored", recovery_id=recovery_id,
                        plan_id=manifest.get("plan_id"))
            return manifest

    def adopt_orphan(
        self, *, media_path: str, video_id: str, checksum_sha256: str,
        evidence_paths: Sequence[str], creator: str, confirm: str,
    ) -> dict[str, Any]:
        if confirm != checksum_sha256:
            raise PathMigrationError("Orphan adoption confirmation must match checksum.")
        relative = self.paths.store(media_path)
        target = self.paths.resolve(relative, must_exist=True, regular=True)
        if target.suffix.lower() not in MEDIA_SUFFIXES:
            raise PathMigrationError("Orphan adoption accepts media files only.")
        if _file_hash(target) != checksum_sha256:
            raise PathMigrationError("Orphan media checksum changed.")
        proven_ids = set()
        validated_evidence = []
        for evidence in evidence_paths:
            evidence_relative = self.paths.store(evidence)
            path = self.paths.resolve(evidence_relative, must_exist=True, regular=True)
            value = _read_json(path)
            evidence_media = value.get("source_media_path") if isinstance(value, dict) else None
            if not isinstance(evidence_media, str):
                raise PathMigrationError("Evidence does not identify its source media.")
            evidence_target = self.paths.resolve(
                evidence_media, must_exist=True, regular=True
            )
            evidence_info = evidence_target.stat()
            target_info = target.stat()
            if (evidence_info.st_dev, evidence_info.st_ino) != (
                target_info.st_dev, target_info.st_ino
            ):
                raise PathMigrationError("Evidence identifies different source media.")
            if isinstance(value, dict) and value.get("video_id"):
                proven_ids.add(value["video_id"])
            validated_evidence.append(evidence_relative)
        if proven_ids != {video_id}:
            raise PathMigrationError("Evidence does not prove exactly one requested video ID.")
        association = {
            "media_path": relative, "video_id": video_id,
            "creator": safe_publication_text(creator, maximum=256),
            "size_bytes": target.stat().st_size,
            "checksum_sha256": checksum_sha256,
            "evidence_paths": sorted(set(validated_evidence)),
        }
        with self._all_locks():
            registry = load_orphan_registry(self.registry_path)
            conflict = next((entry for entry in registry["entries"]
                             if entry["media_path"] == relative), None)
            if conflict:
                if conflict["video_id"] != video_id:
                    raise PathMigrationError("Orphan path already has conflicting ownership.")
                return conflict
            registry["entries"].append(_registry_entry(
                association, adopted_at=utc_now(),
                source="manual_verified_evidence",
            ))
            registry["updated_at"] = utc_now()
            _atomic_json(self.registry_path, registry)
            self._audit("orphan_adopted", media_path=relative, video_id=video_id)
            return registry["entries"][-1]

    def _orphan_analysis(self) -> dict[str, Any]:
        manifest_path = self.data_root / "manifests" / "videos.json"
        manifest = _read_json(manifest_path) if manifest_path.exists() else {"videos": []}
        records = {item["video_id"]: item for item in manifest.get("videos", [])}
        mapped: set[tuple[int, int]] = set()
        for record in records.values():
            value = record.get("local_file_path")
            if not value:
                continue
            try:
                path = self.paths.resolve(value, must_exist=True, regular=True)
                info = path.stat()
                mapped.add((info.st_dev, info.st_ino))
            except (OSError, PersistentPathError):
                continue
        evidence_by_identity: dict[tuple[int, int], list[dict[str, str]]] = defaultdict(list)
        for pattern in (
            "transcripts/*/transcript.json", "clip_candidates/*/candidates.json",
            "previews/**/preview.json",
        ):
            for evidence_path in self.data_root.glob(pattern):
                try:
                    value = _read_json(evidence_path)
                    media_value = value.get("source_media_path")
                    video_id = value.get("video_id")
                    if not isinstance(media_value, str) or not isinstance(video_id, str):
                        continue
                    media = self.paths.resolve(media_value, must_exist=True, regular=True)
                    info = media.stat()
                    evidence_by_identity[(info.st_dev, info.st_ino)].append({
                        "video_id": video_id,
                        "evidence_path": evidence_path.relative_to(self.data_root).as_posix(),
                        "evidence_schema": _schema_for(evidence_path, self.data_root)[0],
                    })
                except (OSError, PersistentPathError, PathMigrationError):
                    continue
        proven, unverified, conflicts = [], [], []
        downloads = self.data_root / "downloads"
        for path in sorted(downloads.rglob("*")) if downloads.is_dir() else []:
            try:
                info = path.lstat()
            except OSError:
                continue
            if path.is_symlink() or not stat.S_ISREG(info.st_mode) or path.suffix.lower() not in MEDIA_SUFFIXES:
                continue
            identity = (info.st_dev, info.st_ino)
            if identity in mapped:
                continue
            evidence = evidence_by_identity.get(identity, [])
            video_ids = {item["video_id"] for item in evidence}
            relative = path.relative_to(self.data_root).as_posix()
            base = {
                "media_path": relative, "size_bytes": info.st_size,
                "checksum_sha256": _file_hash(path),
                "target_identity": {"device": info.st_dev, "inode": info.st_ino},
                "owner_uid": info.st_uid,
                "evidence_paths": sorted({item["evidence_path"] for item in evidence}),
                "artifact_evidence": sorted(
                    evidence, key=lambda item: (item["evidence_schema"], item["evidence_path"])
                ),
            }
            if len(video_ids) > 1:
                conflicts.append({**base, "conflicting_video_ids": sorted(video_ids)})
            elif len(video_ids) == 1 and next(iter(video_ids)) in records:
                video_id = next(iter(video_ids))
                record = records[video_id]
                proven.append({
                    **base, "video_id": video_id,
                    "creator": record.get("channel_name") or record.get("uploader") or "unknown",
                    "dependencies": self._dependency_summary(video_id, evidence),
                })
            else:
                unverified.append({
                    **base, "candidate_video_ids": sorted(video_ids),
                    "reason": "No unique artifact lineage plus manifest identity proves ownership.",
                })
        return {
            "proven_associations": proven,
            "orphaned_unverified": unverified,
            "conflicting_ownership": conflicts,
        }

    def _dependency_summary(
        self, video_id: str, evidence: Sequence[Mapping[str, str]],
    ) -> dict[str, Any]:
        by_schema = defaultdict(list)
        for item in evidence:
            by_schema[item["evidence_schema"]].append(item["evidence_path"])
        queue_path = self.data_root / "review_queue" / "reviews.json"
        queue = _read_json(queue_path) if queue_path.exists() else {"items": []}
        reviews = [
            item.get("review_id") for item in queue.get("items", [])
            if item.get("video_id") == video_id
        ]
        publication_path = self.data_root / "publication" / "records.json"
        publications = (
            _read_json(publication_path) if publication_path.exists()
            else {"attempts": []}
        )
        attempts = [
            item.get("attempt_id") for item in publications.get("attempts", [])
            if item.get("source_video_id") == video_id
            or item.get("review_id") in reviews
        ]
        reference_ids = []
        index_path = self.data_root / "reference_clips" / "index.json"
        index = _read_json(index_path) if index_path.exists() else {"references": []}
        for entry in index.get("references", []):
            try:
                baseline_path = self.paths.resolve(
                    entry.get("baseline_path"), must_exist=True, regular=True
                )
                baseline = _read_json(baseline_path)
            except (OSError, PersistentPathError, PathMigrationError):
                continue
            if baseline.get("source_video_id") == video_id:
                reference_ids.append(entry.get("reference_id"))
        recovery_paths = []
        for path in _immutable_json_files(self.data_root):
            try:
                documents = _read_inventory_documents(path)
            except PathMigrationError:
                continue
            if any(_contains_scalar(document, video_id) for _, document in documents):
                recovery_paths.append(path.relative_to(self.data_root).as_posix())
        return {
            "download_or_processing_manifest": "manifests/videos.json",
            "transcripts": sorted(by_schema.get("transcript_artifact", [])),
            "candidate_artifacts": sorted(by_schema.get("candidate_artifact", [])),
            "preview_artifacts": sorted(by_schema.get("preview_metadata", [])),
            "review_ids": sorted(item for item in reviews if isinstance(item, str)),
            "publication_attempt_ids": sorted(
                item for item in attempts if isinstance(item, str)
            ),
            "reference_ids": sorted(
                item for item in reference_ids if isinstance(item, str)
            ),
            "recovery_records": sorted(recovery_paths),
        }

    def _recovery_for_plan(self, plan_id: str) -> dict[str, Any] | None:
        if not self.recovery_root.is_dir():
            return None
        for path in self.recovery_root.glob("pathrecovery_*/manifest.json"):
            try:
                value = _read_json(path)
            except (OSError, PathMigrationError):
                continue
            if value.get("plan_id") == plan_id:
                return value
        return None

    @contextmanager
    def _all_locks(self) -> Iterator[None]:
        lock_paths = sorted({
            self.lock_path,
            self.data_root / "production" / "production.lock",
            self.data_root / "review_queue" / "reviews.json.lock",
            self.data_root / "publication" / "records.json.lock",
            self.data_root / "media_cleanup" / "cleanup.lock",
            self.data_root / "reference_discovery" / ".decision.lock",
            self.data_root / "reference_annotations" / ".evidence.lock",
        }, key=str)
        streams = []
        try:
            for path in lock_paths:
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                stream = path.open("a+")
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                streams.append(stream)
            yield
        finally:
            for stream in reversed(streams):
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                stream.close()

    def _audit(self, event: str, **fields: Any) -> None:
        value: dict[str, Any] = {
            "version": 1, "timestamp": utc_now(),
            "event": safe_publication_text(event, maximum=100),
        }
        for key, item in fields.items():
            if key.casefold() in {"token", "secret", "authorization", "cookie", "contents"}:
                continue
            value[key] = safe_publication_text(item) if isinstance(item, str) else item
        self.audit_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.audit_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())


def build_media_coverage(
    data_root: Path, *, resolver: PersistentPathResolver | None = None,
    projected_adoptions: Sequence[Mapping[str, Any]] = (),
    cleanup_items: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Account once for every regular media inode under managed media roots."""

    root = Path(data_root).resolve()
    paths = resolver or PersistentPathResolver(root)
    manifest_path = root / "manifests" / "videos.json"
    queue_path = root / "review_queue" / "reviews.json"
    reference_path = root / "reference_clips" / "index.json"
    manifest = _read_json(manifest_path) if manifest_path.exists() else {"videos": []}
    queue = _read_json(queue_path) if queue_path.exists() else {"items": []}
    references = _read_json(reference_path) if reference_path.exists() else {"references": []}
    registry = load_orphan_registry(root / "path_migration" / "orphan_ownership.json")
    mappings: dict[tuple[int, int], list[str]] = defaultdict(list)
    missing = []

    def mapped(value: Any, owner: str) -> None:
        if not isinstance(value, str) or not value:
            return
        try:
            target = paths.resolve(value, must_exist=True, regular=True)
            info = target.stat()
            mappings[(info.st_dev, info.st_ino)].append(owner)
        except (OSError, PersistentPathError) as error:
            missing.append({"owner": owner, "stored_path": value,
                            "reason": safe_publication_text(error)})

    for record in manifest.get("videos", []):
        mapped(record.get("local_file_path"), f"manifest:{record.get('video_id')}")
    for review in queue.get("items", []):
        mapped(review.get("preview_path"), f"review:{review.get('review_id')}")
    for entry in registry["entries"] + list(projected_adoptions):
        mapped(entry.get("media_path"), f"orphan-adoption:{entry.get('video_id')}")
    reference_identities: set[tuple[int, int]] = set()
    for entry in references.get("references", []):
        try:
            target = paths.resolve(entry.get("media_path"), must_exist=True, regular=True)
            info = target.stat()
            reference_identities.add((info.st_dev, info.st_ino))
        except (OSError, PersistentPathError):
            continue
    cleanup = {item.get("relative_path"): bool(item.get("eligible"))
               for item in cleanup_items if item.get("relative_path")}
    roots = {
        "downloads": root / "downloads", "previews": root / "previews",
        "references": root / "reference_clips",
        "discovery": root / "reference_discovery" / "media",
        "evidence_recovery": root / "reference_evidence_recovery",
        "withdrawal_recovery": root / "reference_discovery" / "withdrawal_recovery",
    }
    by_inode: dict[tuple[int, int], dict[str, Any]] = {}
    unsafe = []
    for root_name, directory in roots.items():
        if not directory.is_dir():
            continue
        for candidate in sorted(directory.rglob("*")):
            try:
                info = candidate.lstat()
            except OSError as error:
                unsafe.append({"path": str(candidate), "reason": safe_publication_text(error)})
                continue
            if candidate.is_symlink() or not stat.S_ISREG(info.st_mode):
                if candidate.is_symlink() or (not stat.S_ISDIR(info.st_mode) and candidate.suffix.lower() in MEDIA_SUFFIXES):
                    unsafe.append({"path": str(candidate), "reason": "symlink or special media file"})
                continue
            if candidate.suffix.lower() not in MEDIA_SUFFIXES:
                continue
            identity = (info.st_dev, info.st_ino)
            relative = candidate.relative_to(root).as_posix()
            existing = by_inode.get(identity)
            if existing:
                existing["duplicate_paths"].append(relative)
                continue
            if identity in reference_identities or root_name == "references":
                category = "reference_protected"
            elif root_name in {"evidence_recovery", "withdrawal_recovery"}:
                category = "recovery_protected"
            elif identity in mappings:
                category = "managed_mapped"
            elif root_name == "downloads":
                category = "orphaned_unverified"
            elif root_name == "discovery":
                category = "unmanaged_ignored"
            else:
                category = "unrecognized"
            by_inode[identity] = {
                "path": relative, "size_bytes": info.st_size,
                "device": info.st_dev, "inode": info.st_ino,
                "category": category, "owners": mappings.get(identity, []),
                "eligible": bool(cleanup.get(relative, False)),
                "duplicate_paths": [],
            }
    files = sorted(by_inode.values(), key=lambda item: item["path"])
    category_totals: dict[str, dict[str, int]] = {}
    for category in (
        "managed_mapped", "reference_protected", "recovery_protected",
        "orphaned_unverified", "unmanaged_ignored", "unrecognized",
    ):
        selected = [item for item in files if item["category"] == category]
        category_totals[category] = {
            "file_count": len(selected),
            "bytes": sum(item["size_bytes"] for item in selected),
            "eligible_bytes": sum(item["size_bytes"] for item in selected if item["eligible"]),
            "ineligible_bytes": sum(item["size_bytes"] for item in selected if not item["eligible"]),
        }
    return {
        "file_count": len(files), "bytes": sum(item["size_bytes"] for item in files),
        "eligible_bytes": sum(item["size_bytes"] for item in files if item["eligible"]),
        "ineligible_bytes": sum(item["size_bytes"] for item in files if not item["eligible"]),
        "protected_bytes": sum(item["size_bytes"] for item in files
                               if item["category"] in {"reference_protected", "recovery_protected"}),
        "orphaned_unverified_bytes": category_totals["orphaned_unverified"]["bytes"],
        "unrecognized_bytes": category_totals["unrecognized"]["bytes"],
        "categories": category_totals, "files": files,
        "unsafe_paths": unsafe, "missing_file_records": missing,
        "duplicate_identity_count": sum(bool(item["duplicate_paths"]) for item in files),
    }
