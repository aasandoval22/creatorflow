import copy
import json
import os
import tempfile
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from backend.services.persistent_paths import (
    PersistentPathError,
    PersistentPathResolver,
    infer_data_root,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST_PATH = PROJECT_ROOT / "data" / "manifests" / "videos.json"
MANIFEST_VERSION = 3
RECORD_FIELDS_V1 = {
    "video_id",
    "source_platform",
    "channel_name",
    "channel_url",
    "video_url",
    "title",
    "uploader",
    "upload_date",
    "duration_seconds",
    "discovered_at",
    "downloaded_at",
    "local_file_path",
    "status",
    "error_message",
}
RECORD_FIELDS_V2 = RECORD_FIELDS_V1 | {"transcription"}
RECORD_FIELDS = RECORD_FIELDS_V2 | {"clip_analysis"}
NULLABLE_FIELDS = {
    "channel_name",
    "channel_url",
    "title",
    "uploader",
    "upload_date",
    "duration_seconds",
    "downloaded_at",
    "local_file_path",
    "error_message",
}
TRANSCRIPTION_FIELDS = {
    "status",
    "model",
    "language",
    "started_at",
    "completed_at",
    "transcript_json_path",
    "transcript_text_path",
    "subtitle_srt_path",
    "error_message",
}
TRANSCRIPTION_NULLABLE_STRING_FIELDS = TRANSCRIPTION_FIELDS - {"status"}
CLIP_ANALYSIS_FIELDS = {
    "status",
    "started_at",
    "completed_at",
    "candidate_count",
    "candidates_json_path",
    "error_message",
}


class ManifestError(ValueError):
    """Raised when a video manifest cannot be read or validated."""


class VideoStatus(str, Enum):
    DISCOVERED = "discovered"
    DOWNLOADED = "downloaded"
    SKIPPED = "skipped"
    FAILED = "failed"


class TranscriptionStatus(str, Enum):
    NOT_STARTED = "not_started"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class ClipAnalysisStatus(str, Enum):
    NOT_STARTED = "not_started"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


def default_transcription() -> dict[str, Any]:
    """Return a fresh default transcription state."""

    return {
        "status": TranscriptionStatus.NOT_STARTED.value,
        "model": None,
        "language": None,
        "started_at": None,
        "completed_at": None,
        "transcript_json_path": None,
        "transcript_text_path": None,
        "subtitle_srt_path": None,
        "error_message": None,
    }


def default_clip_analysis() -> dict[str, Any]:
    """Return a fresh default clip-analysis state."""

    return {
        "status": ClipAnalysisStatus.NOT_STARTED.value,
        "started_at": None,
        "completed_at": None,
        "candidate_count": 0,
        "candidates_json_path": None,
        "error_message": None,
    }


def utc_now() -> str:
    """Return the current time as a UTC ISO 8601 timestamp."""

    return datetime.now(timezone.utc).isoformat()


class VideoManifest:
    """Store validated video ingestion records in an atomic JSON manifest."""

    def __init__(
        self, path: Path = DEFAULT_MANIFEST_PATH, *,
        data_root: Path | None = None,
        path_resolver: PersistentPathResolver | None = None,
    ) -> None:
        self.path = Path(path)
        self.paths = path_resolver or PersistentPathResolver(
            data_root or infer_data_root(self.path, "manifests")
        )
        if not self.path.exists():
            self._write_document(self._empty_document())
        else:
            document = self._read_document()
            if isinstance(document, dict) and document.get("version") in (1, 2):
                source_version = document["version"]
                self._validate_document(document, version=source_version)
                videos = []
                for record in document["videos"]:
                    migrated = copy.deepcopy(record)
                    if source_version == 1:
                        migrated["transcription"] = default_transcription()
                    migrated["clip_analysis"] = default_clip_analysis()
                    videos.append(migrated)
                document = {"version": MANIFEST_VERSION, "videos": videos}
                self._write_document(document)
            else:
                self._validate_document(document)

    @staticmethod
    def _empty_document() -> dict[str, Any]:
        return {"version": MANIFEST_VERSION, "videos": []}

    def _read_document(self) -> dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as manifest_file:
                return json.load(manifest_file)
        except json.JSONDecodeError as error:
            raise ManifestError(
                f"Invalid JSON in video manifest {self.path}: {error}. "
                "Repair or remove the file before retrying."
            ) from error

    def read_records(self) -> list[dict[str, Any]]:
        """Read, validate, and return all manifest records."""

        document = self._read_document()
        self._validate_document(document)
        return [self._materialize_record(record) for record in document["videos"]]

    def get(self, video_id: str) -> dict[str, Any] | None:
        """Return one video record, if present."""

        return next(
            (
                record
                for record in self.read_records()
                if record["video_id"] == video_id
            ),
            None,
        )

    def upsert(self, record: dict[str, Any]) -> dict[str, Any]:
        """Insert or replace a record while preserving durable state."""

        candidate = copy.deepcopy(record)
        records = copy.deepcopy(self._read_document()["videos"])
        existing_index = next(
            (
                index
                for index, existing in enumerate(records)
                if existing["video_id"] == candidate.get("video_id")
            ),
            None,
        )

        if "transcription" not in candidate:
            candidate["transcription"] = default_transcription()
        if "clip_analysis" not in candidate:
            candidate["clip_analysis"] = default_clip_analysis()
        if existing_index is not None:
            existing = records[existing_index]
            candidate["discovered_at"] = existing["discovered_at"]
            candidate["transcription"] = copy.deepcopy(
                existing["transcription"]
            )
            candidate["clip_analysis"] = copy.deepcopy(
                existing["clip_analysis"]
            )

        candidate = self._canonicalize_record_paths(
            candidate, previous=existing if existing_index is not None else None
        )

        self._validate_record(candidate, "new record", RECORD_FIELDS)
        if existing_index is None:
            records.append(candidate)
        else:
            records[existing_index] = candidate

        self._write_document(
            {"version": MANIFEST_VERSION, "videos": records}
        )
        return self._materialize_record(candidate)

    def update_transcription(
        self, video_id: str, **changes: Any
    ) -> dict[str, Any]:
        """Update selected transcription fields without replacing metadata."""

        unknown = set(changes) - TRANSCRIPTION_FIELDS
        if unknown:
            raise ManifestError(
                "Unknown transcription fields: "
                f"{', '.join(sorted(unknown))}."
            )
        records = copy.deepcopy(self._read_document()["videos"])
        record = next(
            (item for item in records if item["video_id"] == video_id), None
        )
        if record is None:
            raise ManifestError(f"Video {video_id!r} was not found.")
        changes = copy.deepcopy(changes)
        for field in (
            "transcript_json_path", "transcript_text_path", "subtitle_srt_path",
        ):
            if changes.get(field) is not None:
                changes[field] = self._store_path(changes[field], field)
        record["transcription"].update(changes)
        self._validate_transcription(
            record["transcription"], f"record {video_id!r}"
        )
        self._write_document(
            {"version": MANIFEST_VERSION, "videos": records}
        )
        return self._materialize_record(record)

    def update_clip_analysis(
        self, video_id: str, **changes: Any
    ) -> dict[str, Any]:
        """Update selected clip-analysis fields without replacing metadata."""

        unknown = set(changes) - CLIP_ANALYSIS_FIELDS
        if unknown:
            raise ManifestError(
                "Unknown clip-analysis fields: "
                f"{', '.join(sorted(unknown))}."
            )
        records = copy.deepcopy(self._read_document()["videos"])
        record = next(
            (item for item in records if item["video_id"] == video_id), None
        )
        if record is None:
            raise ManifestError(f"Video {video_id!r} was not found.")
        changes = copy.deepcopy(changes)
        if changes.get("candidates_json_path") is not None:
            changes["candidates_json_path"] = self._store_path(
                changes["candidates_json_path"], "candidates_json_path"
            )
        record["clip_analysis"].update(changes)
        self._validate_clip_analysis(
            record["clip_analysis"], f"record {video_id!r}"
        )
        self._write_document(
            {"version": MANIFEST_VERSION, "videos": records}
        )
        return self._materialize_record(record)

    def _store_path(self, value: Any, field: str) -> str:
        try:
            return self.paths.store(value)
        except (OSError, PersistentPathError) as error:
            raise ManifestError(
                f"Manifest field {field!r} is not a managed persistent path: {error}"
            ) from error

    def _preserve_or_store_path(
        self, value: Any, previous: Any, field: str,
    ) -> str:
        if isinstance(previous, str):
            current = self.paths.classify(os.fspath(value))
            prior = self.paths.classify(previous)
            if (
                current.relative_path is not None
                and current.relative_path == prior.relative_path
            ):
                return previous
        return self._store_path(value, field)

    def _canonicalize_record_paths(
        self, record: dict[str, Any], *, previous: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = copy.deepcopy(record)
        previous = previous or {}
        if result.get("local_file_path") is not None:
            result["local_file_path"] = self._preserve_or_store_path(
                result["local_file_path"], previous.get("local_file_path"),
                "local_file_path",
            )
        transcription = result.get("transcription") or {}
        prior_transcription = previous.get("transcription") or {}
        for field in (
            "transcript_json_path", "transcript_text_path", "subtitle_srt_path",
        ):
            if transcription.get(field) is not None:
                transcription[field] = self._preserve_or_store_path(
                    transcription[field], prior_transcription.get(field), field
                )
        analysis = result.get("clip_analysis") or {}
        prior_analysis = previous.get("clip_analysis") or {}
        if analysis.get("candidates_json_path") is not None:
            analysis["candidates_json_path"] = self._preserve_or_store_path(
                analysis["candidates_json_path"],
                prior_analysis.get("candidates_json_path"),
                "candidates_json_path",
            )
        return result

    def _materialize_record(self, record: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(record)
        try:
            if result.get("local_file_path") is not None:
                result["local_file_path"] = str(
                    self.paths.materialize(result["local_file_path"])
                )
            transcription = result.get("transcription") or {}
            for field in (
                "transcript_json_path", "transcript_text_path", "subtitle_srt_path",
            ):
                if transcription.get(field) is not None:
                    transcription[field] = str(self.paths.materialize(transcription[field]))
            analysis = result.get("clip_analysis") or {}
            if analysis.get("candidates_json_path") is not None:
                analysis["candidates_json_path"] = str(
                    self.paths.materialize(analysis["candidates_json_path"])
                )
        except PersistentPathError as error:
            raise ManifestError(f"Manifest contains an unsafe active path: {error}") from error
        return result

    def _write_document(self, document: dict[str, Any]) -> None:
        self._validate_document(document)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                json.dump(document, temporary_file, indent=2)
                temporary_file.write("\n")
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, self.path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _validate_document(
        self, document: Any, version: int = MANIFEST_VERSION
    ) -> None:
        if not isinstance(document, dict):
            raise ManifestError(
                f"Video manifest {self.path} must be a JSON object."
            )
        if set(document) != {"version", "videos"}:
            raise ManifestError(
                f"Video manifest {self.path} must contain exactly "
                "'version' and 'videos'."
            )
        if document["version"] != version:
            raise ManifestError(
                f"Video manifest {self.path} has unsupported version "
                f"{document['version']!r}; expected {MANIFEST_VERSION}."
            )
        if not isinstance(document["videos"], list):
            raise ManifestError(
                f"Video manifest {self.path} field 'videos' must be a list."
            )

        fields = {
            1: RECORD_FIELDS_V1,
            2: RECORD_FIELDS_V2,
            3: RECORD_FIELDS,
        }.get(version, RECORD_FIELDS)
        seen_ids: set[str] = set()
        for index, record in enumerate(document["videos"], start=1):
            self._validate_record(record, f"record {index}", fields)
            if record["video_id"] in seen_ids:
                raise ManifestError(
                    f"Video manifest {self.path} contains duplicate video_id "
                    f"{record['video_id']!r}."
                )
            seen_ids.add(record["video_id"])

    def _validate_record(
        self, record: Any, label: str, expected_fields: set[str]
    ) -> None:
        if not isinstance(record, dict):
            raise ManifestError(
                f"Video manifest {self.path} {label} must be a JSON object."
            )
        missing = expected_fields - set(record)
        extra = set(record) - expected_fields
        if missing or extra:
            details = []
            if missing:
                details.append(f"missing fields: {', '.join(sorted(missing))}")
            if extra:
                details.append(f"unknown fields: {', '.join(sorted(extra))}")
            raise ManifestError(
                f"Video manifest {self.path} {label} is malformed "
                f"({'; '.join(details)})."
            )

        for field in RECORD_FIELDS_V1 - NULLABLE_FIELDS:
            if not isinstance(record[field], str) or not record[field].strip():
                raise ManifestError(
                    f"Video manifest {self.path} {label} field "
                    f"{field!r} must be a non-blank string."
                )
        for field in NULLABLE_FIELDS - {"duration_seconds"}:
            value = record[field]
            if value is not None and not isinstance(value, str):
                raise ManifestError(
                    f"Video manifest {self.path} {label} field "
                    f"{field!r} must be a string or null."
                )
        duration = record["duration_seconds"]
        if (
            duration is not None
            and (
                isinstance(duration, bool)
                or not isinstance(duration, (int, float))
                or duration < 0
            )
        ):
            raise ManifestError(
                f"Video manifest {self.path} {label} field "
                "'duration_seconds' must be a non-negative number or null."
            )
        try:
            VideoStatus(record["status"])
        except ValueError as error:
            supported = ", ".join(status.value for status in VideoStatus)
            raise ManifestError(
                f"Video manifest {self.path} {label} has invalid status "
                f"{record['status']!r}; expected one of: {supported}."
            ) from error
        for field in ("discovered_at", "downloaded_at"):
            if record[field] is not None:
                self._validate_timestamp(record[field], label, field)
        if "transcription" in expected_fields:
            self._validate_transcription(record["transcription"], label)
        if "clip_analysis" in expected_fields:
            self._validate_clip_analysis(record["clip_analysis"], label)

    def _validate_transcription(self, value: Any, label: str) -> None:
        if not isinstance(value, dict):
            raise ManifestError(
                f"Video manifest {self.path} {label} field "
                "'transcription' must be an object."
            )
        if set(value) != TRANSCRIPTION_FIELDS:
            raise ManifestError(
                f"Video manifest {self.path} {label} transcription "
                "structure is malformed."
            )
        try:
            TranscriptionStatus(value["status"])
        except (TypeError, ValueError) as error:
            supported = ", ".join(
                status.value for status in TranscriptionStatus
            )
            raise ManifestError(
                f"Video manifest {self.path} {label} has invalid "
                f"transcription status {value['status']!r}; expected one of: "
                f"{supported}."
            ) from error
        for field in TRANSCRIPTION_NULLABLE_STRING_FIELDS:
            field_value = value[field]
            if field_value is not None and not isinstance(field_value, str):
                raise ManifestError(
                    f"Video manifest {self.path} {label} transcription field "
                    f"{field!r} must be a string or null."
                )
        for field in ("started_at", "completed_at"):
            if value[field] is not None:
                self._validate_timestamp(value[field], label, field)

    def _validate_clip_analysis(self, value: Any, label: str) -> None:
        if not isinstance(value, dict):
            raise ManifestError(
                f"Video manifest {self.path} {label} field "
                "'clip_analysis' must be an object."
            )
        if set(value) != CLIP_ANALYSIS_FIELDS:
            raise ManifestError(
                f"Video manifest {self.path} {label} clip_analysis "
                "structure is malformed."
            )
        try:
            ClipAnalysisStatus(value["status"])
        except (TypeError, ValueError) as error:
            supported = ", ".join(
                status.value for status in ClipAnalysisStatus
            )
            raise ManifestError(
                f"Video manifest {self.path} {label} has invalid "
                f"clip-analysis status {value['status']!r}; expected one of: "
                f"{supported}."
            ) from error
        count = value["candidate_count"]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ManifestError(
                f"Video manifest {self.path} {label} clip_analysis field "
                "'candidate_count' must be a non-negative integer."
            )
        for field in ("candidates_json_path", "error_message"):
            field_value = value[field]
            if field_value is not None and not isinstance(field_value, str):
                raise ManifestError(
                    f"Video manifest {self.path} {label} clip_analysis field "
                    f"{field!r} must be a string or null."
                )
        for field in ("started_at", "completed_at"):
            if value[field] is not None:
                self._validate_timestamp(value[field], label, field)

    def _validate_timestamp(
        self, value: str, label: str, field: str
    ) -> None:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as error:
            raise ManifestError(
                f"Video manifest {self.path} {label} field {field!r} "
                "must be an ISO 8601 timestamp."
            ) from error
        if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(
            parsed
        ):
            raise ManifestError(
                f"Video manifest {self.path} {label} field {field!r} "
                "must use UTC."
            )
