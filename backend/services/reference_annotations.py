"""Strict, versioned human annotations for accepted local references."""

from __future__ import annotations

import copy
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from backend.services.reference_clip_library import PROJECT_ROOT, _atomic_json
from backend.services.reference_decision_audit import configured_reviewer_name
from backend.services.video_manifest import utc_now


DEFAULT_ANNOTATION_ROOT = PROJECT_ROOT / "data" / "reference_annotations"
ANNOTATION_VERSION = 1
REFERENCE_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")

ENUM_FIELDS = {
    "composition": frozenset({
        "unknown", "full_screen_gameplay", "stacked_gameplay_facecam",
        "facecam_overlay", "split_screen", "centered_landscape_background",
        "other",
    }),
    "facecam_presence": frozenset({"unknown", "none", "small", "prominent"}),
    "opening_style": frozenset({
        "unknown", "immediate_action", "spoken_hook", "text_hook",
        "setup_context", "mid_action_opening",
    }),
    "clip_purpose": frozenset({
        "unknown", "funny_moment", "reaction", "clutch_highlight",
        "explanation", "story", "argument_opinion", "discovery_reveal", "other",
    }),
    "pacing": frozenset({"unknown", "slow", "moderate", "fast", "very_fast"}),
    "payoff_type": frozenset({
        "unknown", "reaction", "gameplay_result", "punchline", "reveal",
        "answer", "escalation", "unresolved",
    }),
    "caption_style": frozenset({
        "unknown", "none", "word_by_word", "phrase_captions",
        "emphasized_keywords",
    }),
}
LIST_FIELDS = ("desired_qualities", "undesirable_qualities")
TEXT_FIELDS = ("reviewer_notes",)
ANNOTATION_FIELDS = tuple(ENUM_FIELDS) + LIST_FIELDS + TEXT_FIELDS


class ReferenceAnnotationError(ValueError):
    """Human reference annotations are malformed or cannot be persisted."""


def _safe_text(value: Any, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise ReferenceAnnotationError("Annotation text values must be strings.")
    cleaned = "".join(
        character if character >= " " and character != "\x7f" else " "
        for character in value
    ).strip()
    if len(cleaned) > maximum:
        raise ReferenceAnnotationError(
            f"Annotation text is limited to {maximum} characters."
        )
    return cleaned


def _timestamp(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise ReferenceAnnotationError("Annotation updated_at is invalid.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ReferenceAnnotationError("Annotation updated_at is invalid.") from error
    if parsed.tzinfo is None:
        raise ReferenceAnnotationError("Annotation updated_at requires a timezone.")


def default_annotation_values() -> dict[str, Any]:
    return {
        **{name: "unknown" for name in ENUM_FIELDS},
        "desired_qualities": [],
        "undesirable_qualities": [],
        "reviewer_notes": "",
    }


class ReferenceAnnotationStore:
    """One strict atomic JSON document per reference; missing means unannotated."""

    def __init__(self, root: Path = DEFAULT_ANNOTATION_ROOT) -> None:
        self.root = Path(root)

    def path(self, reference_id: str) -> Path:
        if not isinstance(reference_id, str) or not REFERENCE_ID_PATTERN.fullmatch(
            reference_id
        ):
            raise ReferenceAnnotationError("Reference ID is invalid.")
        return self.root / f"{reference_id}.json"

    def default(self, reference_id: str) -> dict[str, Any]:
        self.path(reference_id)
        return {
            "version": ANNOTATION_VERSION,
            "reference_id": reference_id,
            "revision": 0,
            "updated_at": None,
            "reviewer": None,
            "annotations": default_annotation_values(),
        }

    def exists(self, reference_id: str) -> bool:
        return self.path(reference_id).is_file()

    def read(self, reference_id: str) -> dict[str, Any]:
        path = self.path(reference_id)
        if not path.exists():
            return self.default(reference_id)
        try:
            import json

            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ReferenceAnnotationError(
                f"Cannot read annotation {path}: {error}."
            ) from error
        self._validate_document(document, expected_reference_id=reference_id)
        return copy.deepcopy(document)

    def update(
        self,
        reference_id: str,
        *,
        expected_revision: int,
        values: Mapping[str, Any],
        reviewer: str | None = None,
    ) -> tuple[dict[str, Any], list[str]]:
        current = self.read(reference_id)
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise ReferenceAnnotationError("Expected annotation revision is invalid.")
        if expected_revision != current["revision"]:
            raise ReferenceAnnotationError(
                f"Stale annotation form: expected revision {expected_revision}, "
                f"current revision is {current['revision']}. Refresh and retry."
            )
        normalized = self._validate_values(values)
        changed = [
            name
            for name in ANNOTATION_FIELDS
            if normalized[name] != current["annotations"][name]
        ]
        document = {
            "version": ANNOTATION_VERSION,
            "reference_id": reference_id,
            "revision": current["revision"] + 1,
            "updated_at": utc_now(),
            "reviewer": configured_reviewer_name(reviewer),
            "annotations": normalized,
        }
        self._validate_document(document, expected_reference_id=reference_id)
        _atomic_json(self.path(reference_id), document)
        return copy.deepcopy(document), changed

    def snapshot(self, reference_id: str) -> bytes | None:
        path = self.path(reference_id)
        return path.read_bytes() if path.exists() else None

    def restore(self, reference_id: str, snapshot: bytes | None) -> None:
        path = self.path(reference_id)
        if snapshot is None:
            path.unlink(missing_ok=True)
            return
        try:
            import json

            document = json.loads(snapshot.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise ReferenceAnnotationError("Annotation rollback snapshot is invalid.") from error
        self._validate_document(document, expected_reference_id=reference_id)
        _atomic_json(path, document)

    @staticmethod
    def _validate_values(values: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(values, Mapping) or set(values) != set(ANNOTATION_FIELDS):
            raise ReferenceAnnotationError(
                "Annotations must contain exactly the supported structured fields."
            )
        normalized: dict[str, Any] = {}
        for name, allowed in ENUM_FIELDS.items():
            value = values[name]
            if not isinstance(value, str) or value not in allowed:
                raise ReferenceAnnotationError(
                    f"Unsupported {name.replace('_', ' ')} annotation {value!r}."
                )
            normalized[name] = value
        for name in LIST_FIELDS:
            value = values[name]
            if not isinstance(value, list) or len(value) > 25:
                raise ReferenceAnnotationError(
                    f"{name.replace('_', ' ').title()} must be a list of at most 25 items."
                )
            items = []
            for item in value:
                cleaned = _safe_text(item, maximum=200)
                if cleaned and cleaned not in items:
                    items.append(cleaned)
            normalized[name] = items
        normalized["reviewer_notes"] = _safe_text(
            values["reviewer_notes"], maximum=4_000
        )
        return normalized

    @classmethod
    def _validate_document(
        cls, document: Any, *, expected_reference_id: str
    ) -> None:
        if (
            not isinstance(document, dict)
            or set(document)
            != {"version", "reference_id", "revision", "updated_at", "reviewer", "annotations"}
            or document["version"] != ANNOTATION_VERSION
            or document["reference_id"] != expected_reference_id
            or isinstance(document["revision"], bool)
            or not isinstance(document["revision"], int)
            or document["revision"] < 0
        ):
            raise ReferenceAnnotationError("Reference annotation document is malformed.")
        _timestamp(document["updated_at"])
        reviewer = document["reviewer"]
        if reviewer is not None:
            _safe_text(reviewer, maximum=100)
        cls._validate_values(document["annotations"])
