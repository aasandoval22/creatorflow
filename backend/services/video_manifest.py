import copy
import json
import os
import tempfile
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST_PATH = PROJECT_ROOT / "data" / "manifests" / "videos.json"
MANIFEST_VERSION = 2
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
RECORD_FIELDS = RECORD_FIELDS_V1 | {"transcription"}
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


def utc_now() -> str:
    """Return the current time as a UTC ISO 8601 timestamp."""

    return datetime.now(timezone.utc).isoformat()


class VideoManifest:
    """Store validated video ingestion records in an atomic JSON manifest."""

    def __init__(self, path: Path = DEFAULT_MANIFEST_PATH) -> None:
        self.path = Path(path)
        if not self.path.exists():
            self._write_document(self._empty_document())
        else:
            document = self._read_document()
            if isinstance(document, dict) and document.get("version") == 1:
                self._validate_document(document, version=1)
                document = {
                    "version": MANIFEST_VERSION,
                    "videos": [
                        {**record, "transcription": default_transcription()}
                        for record in document["videos"]
                    ],
                }
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
        return copy.deepcopy(document["videos"])

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
        records = self.read_records()
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
        if existing_index is not None:
            existing = records[existing_index]
            candidate["discovered_at"] = existing["discovered_at"]
            candidate["transcription"] = copy.deepcopy(
                existing["transcription"]
            )

        self._validate_record(candidate, "new record", RECORD_FIELDS)
        if existing_index is None:
            records.append(candidate)
        else:
            records[existing_index] = candidate

        self._write_document(
            {"version": MANIFEST_VERSION, "videos": records}
        )
        return copy.deepcopy(candidate)

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
        records = self.read_records()
        record = next(
            (item for item in records if item["video_id"] == video_id), None
        )
        if record is None:
            raise ManifestError(f"Video {video_id!r} was not found.")
        record["transcription"].update(copy.deepcopy(changes))
        self._validate_transcription(
            record["transcription"], f"record {video_id!r}"
        )
        self._write_document(
            {"version": MANIFEST_VERSION, "videos": records}
        )
        return copy.deepcopy(record)

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

        fields = RECORD_FIELDS_V1 if version == 1 else RECORD_FIELDS
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
