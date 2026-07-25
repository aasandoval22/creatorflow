"""Strict, local-only registry for accepted clip references."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.services.video_manifest import utc_now

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REFERENCE_ROOT = PROJECT_ROOT / "data" / "reference_clips"
DEFAULT_REFERENCE_INDEX = DEFAULT_REFERENCE_ROOT / "index.json"
INDEX_VERSION = 1
ALLOWED_STATUSES = frozenset({"accepted", "pending", "rejected", "archived"})
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PROFILE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class ReferenceClipError(ValueError):
    """A reference or registry is invalid and needs user action."""


def _atomic_json(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.",
            suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(document, stream, indent=2, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _string(value: Any, label: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ReferenceClipError(f"{label} must be a nonempty string.")
    if "\x00" in value or any(ord(character) < 32 and character not in "\t\n\r" for character in value):
        raise ReferenceClipError(f"{label} contains unsupported control characters.")
    return value


def _timestamp(value: Any, label: str) -> None:
    _string(value, label)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ReferenceClipError(f"{label} must be a UTC ISO timestamp.") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ReferenceClipError(f"{label} must include the UTC timezone.")


def load_and_validate_baseline(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as stream:
            value = json.load(stream)
    except json.JSONDecodeError as error:
        raise ReferenceClipError(f"Invalid JSON in baseline {path}: {error}.") from error
    except OSError as error:
        raise ReferenceClipError(f"Cannot read baseline {path}: {error}.") from error
    if not isinstance(value, dict):
        raise ReferenceClipError("Baseline must contain a JSON object.")
    allowed = {
        "version", "reference_id", "source_video_id", "source_title", "creator",
        "status", "purpose", "profile_name", "qualities", "layout",
        "story_structure", "timing_preferences", "notes",
    }
    unknown = set(value) - allowed
    if unknown:
        raise ReferenceClipError(f"Baseline has unsupported fields: {', '.join(sorted(unknown))}.")
    required = {"version", "reference_id", "source_video_id", "source_title", "creator",
                "status", "purpose", "qualities", "layout", "timing_preferences", "notes"}
    missing = required - set(value)
    if missing:
        raise ReferenceClipError(f"Baseline is missing fields: {', '.join(sorted(missing))}.")
    if value["version"] != 1:
        raise ReferenceClipError("Baseline version must be 1.")
    for name in ("reference_id", "source_video_id", "source_title", "creator", "purpose", "notes"):
        _string(value[name], f"Baseline {name}")
    if not _ID.fullmatch(value["reference_id"]):
        raise ReferenceClipError("Baseline reference_id contains unsupported characters.")
    if value["status"] not in ALLOWED_STATUSES:
        raise ReferenceClipError(f"Unsupported baseline status: {value['status']!r}.")
    if "profile_name" in value and value["profile_name"] is not None:
        if not isinstance(value["profile_name"], str) or not _PROFILE.fullmatch(value["profile_name"]):
            raise ReferenceClipError("Baseline profile_name is invalid.")
    qualities = value["qualities"]
    if not isinstance(qualities, list) or not qualities:
        raise ReferenceClipError("Baseline qualities must be a nonempty list.")
    for index, quality in enumerate(qualities):
        _string(quality, f"Baseline quality {index}")
    _validate_mapping(
        value["layout"], "layout",
        {"orientation", "composition", "top_region", "bottom_region", "facecam_prominence"},
        required={"orientation", "top_region", "bottom_region"},
    )
    _validate_mapping(
        value["timing_preferences"], "timing_preferences",
        {"requires_long_lead_in", "requires_complete_setup", "requires_complete_payoff", "preferred_ending"},
        required={"requires_long_lead_in", "requires_complete_setup", "requires_complete_payoff", "preferred_ending"},
        booleans={"requires_long_lead_in", "requires_complete_setup", "requires_complete_payoff"},
    )
    if "story_structure" in value:
        _validate_mapping(
            value["story_structure"], "story_structure",
            {"opening_style", "setup_requirement", "primary_focus", "payoff_type",
             "payoff_required", "ending_style"},
            booleans={"payoff_required"},
        )
    return copy.deepcopy(value)


def _validate_mapping(
    value: Any, label: str, allowed: set[str], *,
    required: set[str] = frozenset(), booleans: set[str] = frozenset(),
) -> None:
    if not isinstance(value, dict):
        raise ReferenceClipError(f"Baseline {label} must be an object.")
    if set(value) - allowed or required - set(value):
        raise ReferenceClipError(f"Baseline {label} has missing or unsupported fields.")
    for name, item in value.items():
        if name in booleans:
            if not isinstance(item, bool):
                raise ReferenceClipError(f"Baseline {label}.{name} must be boolean.")
        else:
            _string(item, f"Baseline {label}.{name}")


def annotation_defaults(baseline: dict[str, Any]) -> dict[str, Any]:
    """Return analysis defaults without mutating the user-authored document."""
    story = baseline.get("story_structure", {})
    timing = baseline["timing_preferences"]
    return {
        "opening_style": story.get(
            "opening_style", "mid_action" if not timing["requires_long_lead_in"] else "setup"
        ),
        "payoff_required": story.get("payoff_required", timing["requires_complete_payoff"]),
        "ending_style": story.get("ending_style", "shortly_after_payoff"),
    }


class ReferenceClipLibrary:
    def __init__(
        self, root: Path = DEFAULT_REFERENCE_ROOT, index_path: Path | None = None
    ) -> None:
        self.root = Path(root)
        self.index_path = Path(index_path) if index_path is not None else self.root / "index.json"
        if self.index_path.exists():
            self._load()

    @staticmethod
    def stable_reference_id(baseline: dict[str, Any], media_path: Path) -> str:
        annotated = baseline.get("reference_id")
        if isinstance(annotated, str) and _ID.fullmatch(annotated):
            return annotated
        source = baseline.get("source_video_id")
        if isinstance(source, str) and source.strip():
            return f"youtube-{source.strip()}"
        return f"local-{sha256_file(media_path)[:20]}"

    def _empty(self) -> dict[str, Any]:
        return {"version": INDEX_VERSION, "updated_at": utc_now(), "references": []}

    def _load(self) -> dict[str, Any]:
        if not self.index_path.exists():
            return self._empty()
        try:
            with self.index_path.open(encoding="utf-8") as stream:
                document = json.load(stream)
        except json.JSONDecodeError as error:
            raise ReferenceClipError(
                f"Reference index {self.index_path} is corrupt: {error}. "
                "Repair or move it before retrying."
            ) from error
        except OSError as error:
            raise ReferenceClipError(f"Cannot read reference index {self.index_path}: {error}.") from error
        self._validate_index(document)
        return document

    def _validate_index(self, document: Any) -> None:
        if not isinstance(document, dict) or set(document) != {"version", "updated_at", "references"}:
            raise ReferenceClipError("Reference index must contain exactly version, updated_at, references.")
        if document["version"] != INDEX_VERSION:
            raise ReferenceClipError(f"Unsupported reference index version {document['version']!r}.")
        _timestamp(document["updated_at"], "Index updated_at")
        if not isinstance(document["references"], list):
            raise ReferenceClipError("Reference index references must be a list.")
        ids, identities = set(), set()
        fields = {"reference_id", "media_path", "source_info_path", "baseline_path",
                  "analysis_path", "checksum_sha256", "status", "profile_name",
                  "creator", "created_at", "updated_at"}
        for index, item in enumerate(document["references"]):
            if not isinstance(item, dict) or set(item) != fields:
                raise ReferenceClipError(f"Reference index entry {index} has missing or unknown fields.")
            for name in ("reference_id", "media_path", "baseline_path", "analysis_path",
                         "checksum_sha256", "status", "profile_name", "creator"):
                _string(item[name], f"Entry {index} {name}")
            _string(item["source_info_path"], f"Entry {index} source_info_path", optional=True)
            if not _ID.fullmatch(item["reference_id"]) or not _PROFILE.fullmatch(item["profile_name"]):
                raise ReferenceClipError(f"Reference index entry {index} has an invalid identity or profile.")
            if not re.fullmatch(r"[0-9a-f]{64}", item["checksum_sha256"]):
                raise ReferenceClipError(f"Reference index entry {index} has an invalid checksum.")
            if item["status"] not in ALLOWED_STATUSES:
                raise ReferenceClipError(f"Reference index entry {index} has an unsupported status.")
            _timestamp(item["created_at"], f"Entry {index} created_at")
            _timestamp(item["updated_at"], f"Entry {index} updated_at")
            identity = str(Path(item["media_path"]).resolve())
            if item["reference_id"] in ids:
                raise ReferenceClipError(f"Duplicate reference_id {item['reference_id']!r} in index.")
            if identity in identities:
                raise ReferenceClipError(f"Duplicate media identity {identity!r} in index.")
            ids.add(item["reference_id"]); identities.add(identity)

    def register(
        self, *, media_path: Path, baseline_path: Path,
        source_info_path: Path | None = None, reference_id: str | None = None,
        profile_name: str | None = None,
    ) -> dict[str, Any]:
        media, baseline_file = Path(media_path).resolve(), Path(baseline_path).resolve()
        source = Path(source_info_path).resolve() if source_info_path is not None else None
        if not media.is_file():
            raise ReferenceClipError(f"Reference media does not exist: {media}")
        if not baseline_file.is_file():
            raise ReferenceClipError(f"Reference baseline does not exist: {baseline_file}")
        if source is not None and not source.is_file():
            raise ReferenceClipError(f"Reference source-info does not exist: {source}")
        baseline = load_and_validate_baseline(baseline_file)
        chosen_id = reference_id or self.stable_reference_id(baseline, media)
        profile = profile_name or baseline.get("profile_name") or "personality_reaction"
        if not isinstance(chosen_id, str) or not _ID.fullmatch(chosen_id):
            raise ReferenceClipError("reference_id is invalid.")
        if baseline["reference_id"] != chosen_id:
            raise ReferenceClipError("reference_id does not match baseline reference_id.")
        if not isinstance(profile, str) or not _PROFILE.fullmatch(profile):
            raise ReferenceClipError("profile_name is invalid.")
        document = self._load()
        resolved = str(media)
        if any(item["reference_id"] == chosen_id for item in document["references"]):
            raise ReferenceClipError(f"Reference ID {chosen_id!r} is already registered.")
        if any(str(Path(item["media_path"]).resolve()) == resolved for item in document["references"]):
            raise ReferenceClipError(f"Media file {media} is already registered.")
        now = utc_now()
        entry = {
            "reference_id": chosen_id, "media_path": resolved,
            "source_info_path": str(source) if source else None,
            "baseline_path": str(baseline_file),
            "analysis_path": str(baseline_file.parent / "analysis.json"),
            "checksum_sha256": sha256_file(media), "status": baseline["status"],
            "profile_name": profile, "creator": baseline["creator"],
            "created_at": now, "updated_at": now,
        }
        document["references"].append(entry)
        document["references"].sort(key=lambda item: item["reference_id"])
        document["updated_at"] = now
        self._validate_index(document)
        _atomic_json(self.index_path, document)
        return copy.deepcopy(entry)

    def register_directory(
        self, directory: Path, *, reference_id: str | None = None,
        profile_name: str | None = None, media_path: Path | None = None,
        baseline_path: Path | None = None, source_info_path: Path | None = None,
    ) -> dict[str, Any]:
        directory = Path(directory)
        media = media_path or directory / "reference.mp4"
        baseline = baseline_path or directory / "baseline.json"
        source = source_info_path
        if source is None and (directory / "reference.info.json").is_file():
            source = directory / "reference.info.json"
        return self.register(
            media_path=media, baseline_path=baseline, source_info_path=source,
            reference_id=reference_id, profile_name=profile_name,
        )

    def list_references(
        self, *, status: str | None = None, creator: str | None = None,
        profile_name: str | None = None,
    ) -> list[dict[str, Any]]:
        if status is not None and status not in ALLOWED_STATUSES:
            raise ReferenceClipError(f"Unsupported status filter: {status!r}.")
        return [
            copy.deepcopy(item) for item in self._load()["references"]
            if (status is None or item["status"] == status)
            and (creator is None or item["creator"].casefold() == creator.casefold())
            and (profile_name is None or item["profile_name"] == profile_name)
        ]

    def get(self, reference_id: str) -> dict[str, Any]:
        for item in self._load()["references"]:
            if item["reference_id"] == reference_id:
                return copy.deepcopy(item)
        raise ReferenceClipError(f"Reference {reference_id!r} is not registered.")

    def validate_checksum(self, reference_id: str) -> bool:
        entry = self.get(reference_id)
        path = Path(entry["media_path"])
        if not path.is_file():
            raise ReferenceClipError(f"Registered media is missing: {path}")
        actual = sha256_file(path)
        if actual != entry["checksum_sha256"]:
            raise ReferenceClipError(
                f"Reference {reference_id!r} changed: expected SHA-256 "
                f"{entry['checksum_sha256']}, found {actual}."
            )
        return True

    def update_annotations(self, reference_id: str) -> dict[str, Any]:
        document = self._load()
        for item in document["references"]:
            if item["reference_id"] == reference_id:
                baseline = load_and_validate_baseline(Path(item["baseline_path"]))
                if baseline["reference_id"] != reference_id:
                    raise ReferenceClipError("Updated baseline reference_id does not match the index.")
                item.update(status=baseline["status"], creator=baseline["creator"], updated_at=utc_now())
                document["updated_at"] = item["updated_at"]
                _atomic_json(self.index_path, document)
                return copy.deepcopy(item)
        raise ReferenceClipError(f"Reference {reference_id!r} is not registered.")

    def remove(self, reference_id: str) -> dict[str, Any]:
        document = self._load()
        matches = [item for item in document["references"] if item["reference_id"] == reference_id]
        if not matches:
            raise ReferenceClipError(f"Reference {reference_id!r} is not registered.")
        document["references"] = [
            item for item in document["references"] if item["reference_id"] != reference_id
        ]
        document["updated_at"] = utc_now()
        _atomic_json(self.index_path, document)
        return copy.deepcopy(matches[0])
