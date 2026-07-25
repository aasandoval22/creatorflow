import json
import os
import tempfile
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST_PATH = PROJECT_ROOT / "data" / "manifests" / "videos.json"
MANIFEST_VERSION = 1
RECORD_FIELDS = {
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


class ManifestError(ValueError):
    """Raised when a video manifest cannot be read or validated."""


class VideoStatus(str, Enum):
    DISCOVERED = "discovered"
    DOWNLOADED = "downloaded"
    SKIPPED = "skipped"
    FAILED = "failed"


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
            self.read_records()

    @staticmethod
    def _empty_document() -> dict[str, Any]:
        return {"version": MANIFEST_VERSION, "videos": []}

    def read_records(self) -> list[dict[str, Any]]:
        """Read and validate all records from the manifest."""

        try:
            with self.path.open("r", encoding="utf-8") as manifest_file:
                document = json.load(manifest_file)
        except json.JSONDecodeError as error:
            raise ManifestError(
                f"Invalid JSON in video manifest {self.path}: {error}. "
                "Repair or remove the file before retrying."
            ) from error

        self._validate_document(document)
        return [record.copy() for record in document["videos"]]

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
        """Insert or replace a record, preserving its first discovery time."""

        candidate = record.copy()
        records = self.read_records()
        existing_index = next(
            (
                index
                for index, existing in enumerate(records)
                if existing["video_id"] == candidate.get("video_id")
            ),
            None,
        )

        if existing_index is not None:
            candidate["discovered_at"] = records[existing_index][
                "discovered_at"
            ]

        self._validate_record(candidate, "new record")

        if existing_index is None:
            records.append(candidate)
        else:
            records[existing_index] = candidate

        self._write_document(
            {"version": MANIFEST_VERSION, "videos": records}
        )
        return candidate.copy()

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

    def _validate_document(self, document: Any) -> None:
        if not isinstance(document, dict):
            raise ManifestError(
                f"Video manifest {self.path} must be a JSON object."
            )

        if set(document) != {"version", "videos"}:
            raise ManifestError(
                f"Video manifest {self.path} must contain exactly "
                "'version' and 'videos'."
            )

        if document["version"] != MANIFEST_VERSION:
            raise ManifestError(
                f"Video manifest {self.path} has unsupported version "
                f"{document['version']!r}; expected {MANIFEST_VERSION}."
            )

        if not isinstance(document["videos"], list):
            raise ManifestError(
                f"Video manifest {self.path} field 'videos' must be a list."
            )

        seen_ids: set[str] = set()
        for index, record in enumerate(document["videos"], start=1):
            self._validate_record(record, f"record {index}")
            if record["video_id"] in seen_ids:
                raise ManifestError(
                    f"Video manifest {self.path} contains duplicate video_id "
                    f"{record['video_id']!r}."
                )
            seen_ids.add(record["video_id"])

    def _validate_record(self, record: Any, label: str) -> None:
        if not isinstance(record, dict):
            raise ManifestError(
                f"Video manifest {self.path} {label} must be a JSON object."
            )

        missing = RECORD_FIELDS - set(record)
        extra = set(record) - RECORD_FIELDS
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

        for field in RECORD_FIELDS - NULLABLE_FIELDS:
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

    def _validate_timestamp(
        self, value: str, label: str, field: str
    ) -> None:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
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
