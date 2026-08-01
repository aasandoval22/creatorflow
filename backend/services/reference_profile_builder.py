"""Build deterministic soft-prior profiles from accepted local references."""

from __future__ import annotations

import json
import statistics
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any

from backend.services.reference_annotations import (
    DEFAULT_ANNOTATION_ROOT,
    ENUM_FIELDS,
    LIST_FIELDS,
    ReferenceAnnotationStore,
)
from backend.services.reference_clip_library import (
    PROJECT_ROOT, ReferenceClipError, ReferenceClipLibrary, _atomic_json,
    load_and_validate_baseline,
)

DEFAULT_PROFILE_ROOT = PROJECT_ROOT / "data" / "reference_profiles"


class ReferenceProfileError(ValueError):
    pass


class ReferenceProfileBuilder:
    def __init__(
        self, library: ReferenceClipLibrary, output_directory: Path = DEFAULT_PROFILE_ROOT,
        *, annotation_store: ReferenceAnnotationStore | None = None,
    ) -> None:
        self.library = library
        self.output_directory = Path(output_directory)
        self.annotation_store = annotation_store or ReferenceAnnotationStore(
            DEFAULT_ANNOTATION_ROOT
        )

    def profile_path(self, profile_name: str) -> Path:
        if "/" in profile_name or "\\" in profile_name or profile_name in {"", ".", ".."}:
            raise ReferenceProfileError("Profile name is invalid.")
        return self.output_directory / f"{profile_name}.json"

    def build(self, profile_name: str) -> dict[str, Any]:
        references = self.library.list_references(status="accepted", profile_name=profile_name)
        if not references:
            raise ReferenceProfileError(f"No accepted references are registered for {profile_name!r}.")
        durations, baselines, ids, analyses, input_versions = [], [], [], [], []
        for entry in references:
            analysis_path = Path(entry["analysis_path"])
            if not analysis_path.is_file():
                raise ReferenceProfileError(
                    f"Reference {entry['reference_id']} has no analysis; run analyze first."
                )
            try:
                analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise ReferenceProfileError(f"Cannot read analysis {analysis_path}: {error}.") from error
            try:
                duration = float(analysis["media"]["duration"])
            except (KeyError, TypeError, ValueError) as error:
                raise ReferenceProfileError(f"Analysis for {entry['reference_id']} lacks duration.") from error
            durations.append(duration)
            analyses.append(analysis)
            baselines.append(load_and_validate_baseline(Path(entry["baseline_path"])))
            ids.append(entry["reference_id"])
            annotation = self.annotation_store.read(entry["reference_id"])
            input_versions.append({
                "reference_id": entry["reference_id"],
                "analysis_revision": self._analysis_revision(analysis),
                "annotation_revision": annotation["revision"],
            })
        ids.sort()
        input_versions.sort(key=lambda item: item["reference_id"])
        median = round(statistics.median(durations), 3)
        observed_min, observed_max = round(min(durations), 3), round(max(durations), 3)
        frame_rates = [
            float(item["media"]["frame_rate"]) for item in analyses
            if item["media"].get("frame_rate") is not None
        ]
        # A bounded soft range: observed values inform it but never become a fixed duration.
        recommended_min = max(10, int(5 * round((observed_min * 0.78) / 5)))
        recommended_target = int(5 * round(median / 5))
        recommended_max = max(recommended_target + 10, int(5 * round((observed_max * 1.45) / 5)))
        timing = [baseline["timing_preferences"] for baseline in baselines]
        layouts = [baseline["layout"] for baseline in baselines]
        stories = [baseline.get("story_structure", {}) for baseline in baselines]
        stacked = sum(
            layout.get("composition") == "stacked"
            or (layout.get("top_region") == "facecam" and layout.get("bottom_region") == "gameplay")
            for layout in layouts
        ) >= len(layouts) / 2
        automatic_evidence = self._automatic_evidence(analyses, durations)
        human_preferences = self._human_preferences(ids)
        document = {
            "version": 2, "profile_name": profile_name, "reference_ids": ids,
            "reference_count": len(ids),
            "confidence": "provisional" if len(ids) == 1 else "multi_reference",
            "duration": {
                "observed_median": median, "observed_range": [observed_min, observed_max],
                "recommended_minimum": recommended_min,
                "recommended_target": recommended_target,
                "recommended_maximum": recommended_max,
            },
            "media": {
                "observed_resolutions": sorted({
                    f"{item['media'].get('width')}x{item['media'].get('height')}"
                    for item in analyses
                }),
                "observed_frame_rate_median": (
                    round(statistics.median(frame_rates), 3) if frame_rates else None
                ),
            },
            "opening": {
                "long_lead_in_required": all(item["requires_long_lead_in"] for item in timing),
                "mid_action_allowed": any(
                    story.get("opening_style") == "mid_action" for story in stories
                ) or any(not item["requires_long_lead_in"] for item in timing),
                "rapid_comprehension_required": True,
            },
            "story": {
                "personality_weight": "high",
                "setup_weight": "context_dependent",
                "payoff_required": any(item["requires_complete_payoff"] for item in timing),
                "complete_story_beat_required": True,
            },
            "ending": {"stop_after_payoff": True, "excess_tail_penalty": True},
            "layout": {
                "preferred_composition": (
                    "stacked_facecam_gameplay" if stacked else "reference_dependent"
                ),
                "facecam_prominence": (
                    "high" if any(layout.get("facecam_prominence") == "high"
                                  or layout.get("top_region") == "facecam" for layout in layouts)
                    else "unspecified"
                ),
            },
            "automatic_evidence": automatic_evidence,
            "human_preferences": human_preferences,
            "input_versions": input_versions,
            "staleness": {"status": "current", "reasons": []},
            "limitations": [
                "This profile is a deterministic soft prior, not an AI training sample.",
                "Duration observations do not prescribe one duration for future clips.",
                "Human annotations are preferences with contributor counts, not universal rules.",
                "Transcript findings are heuristic evidence, not proof of humor or quality.",
                "Human review determines whether a clip is publishable.",
            ],
        }
        _atomic_json(self.profile_path(profile_name), document)
        return document

    def read(self, profile_name: str) -> dict[str, Any]:
        path = self.profile_path(profile_name)
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise ReferenceProfileError(f"Profile {profile_name!r} has not been built.") from error
        except (OSError, json.JSONDecodeError) as error:
            raise ReferenceProfileError(f"Cannot read profile {path}: {error}.") from error
        if (
            not isinstance(document, dict) or document.get("version") not in {1, 2}
            or document.get("profile_name") != profile_name
        ):
            raise ReferenceProfileError(f"Profile {path} is malformed.")
        if document["version"] == 1:
            return document
        return self._with_staleness(document)

    def mark_stale(
        self, reference_id: str, *, reason: str
    ) -> dict[Path, bytes]:
        """Persist a stale marker without rebuilding any profile evidence."""

        snapshots: dict[Path, bytes] = {}
        if not self.output_directory.exists():
            return snapshots
        for path in sorted(self.output_directory.glob("*.json")):
            try:
                raw = path.read_bytes()
                document = json.loads(raw.decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ReferenceProfileError(
                    f"Cannot inspect profile {path} for staleness: {error}."
                ) from error
            ids = document.get("reference_ids")
            if not isinstance(ids, list) or not all(
                isinstance(value, str) for value in ids
            ):
                raise ReferenceProfileError(f"Profile {path} has malformed reference IDs.")
            if reference_id not in ids:
                continue
            previous = document.get("staleness")
            reasons = (
                list(previous.get("reasons", []))
                if isinstance(previous, dict)
                and isinstance(previous.get("reasons"), list)
                else []
            )
            if reason not in reasons:
                reasons.append(reason)
            document["staleness"] = {"status": "stale", "reasons": reasons}
            snapshots[path] = raw
            _atomic_json(path, document)
        return snapshots

    @staticmethod
    def _analysis_revision(document: dict[str, Any]) -> int:
        value = document.get("analysis_revision", 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ReferenceProfileError("Reference analysis revision is invalid.")
        return value

    @staticmethod
    def _automatic_evidence(
        analyses: list[dict[str, Any]], durations: list[float]
    ) -> dict[str, Any]:
        frame_rates = [
            float(item["media"]["frame_rate"])
            for item in analyses
            if item.get("media", {}).get("frame_rate") is not None
        ]
        scene_counts = [
            len(item.get("visual_timing", {}).get("scene_change_timestamps", []))
            for item in analyses
        ]
        silence_counts = [
            len(item.get("audio_timing", {}).get("silence_intervals", []))
            for item in analyses
        ]
        transcript_analyses = [
            item for item in analyses
            if item.get("transcription", {}).get("status") == "available"
            or bool(item.get("speech", {}).get("words"))
        ]
        word_counts = [
            int(item.get("speech", {}).get("word_count", 0))
            for item in transcript_analyses
        ]
        speech_densities = [
            float(item.get("speech", {}).get("speech_density", 0))
            for item in transcript_analyses
            if item.get("speech", {}).get("speech_density") is not None
        ]
        return {
            "duration": {
                "evidence_kind": "observed_metric",
                "contributor_count": len(durations),
                "median": round(statistics.median(durations), 3),
                "range": [round(min(durations), 3), round(max(durations), 3)],
            },
            "frame_rate": {
                "evidence_kind": "observed_metric",
                "contributor_count": len(frame_rates),
                "median": round(statistics.median(frame_rates), 3) if frame_rates else None,
            },
            "scene_changes": {
                "evidence_kind": "pixel_change_signal",
                "contributor_count": len(scene_counts),
                "median_count": round(statistics.median(scene_counts), 3),
                "range": [min(scene_counts), max(scene_counts)],
            },
            "silence_intervals": {
                "evidence_kind": "audio_timing_signal",
                "contributor_count": len(silence_counts),
                "median_count": round(statistics.median(silence_counts), 3),
                "range": [min(silence_counts), max(silence_counts)],
            },
            "speech": {
                "evidence_kind": (
                    "transcript_heuristic" if transcript_analyses else "unavailable"
                ),
                "contributor_count": len(transcript_analyses),
                "unavailable_count": len(analyses) - len(transcript_analyses),
                "median_word_count": (
                    round(statistics.median(word_counts), 3) if word_counts else None
                ),
                "median_speech_density": (
                    round(statistics.median(speech_densities), 4)
                    if speech_densities else None
                ),
            },
        }

    def _human_preferences(self, reference_ids: list[str]) -> dict[str, Any]:
        annotations = [
            self.annotation_store.read(reference_id)["annotations"]
            for reference_id in reference_ids
            if self.annotation_store.exists(reference_id)
        ]
        minimum = 2
        fields: dict[str, Any] = {}
        for name in ENUM_FIELDS:
            values = [item[name] for item in annotations if item[name] != "unknown"]
            contributors = len(values)
            fields[name] = {
                "evidence_kind": (
                    "human_preference" if contributors >= minimum else "unavailable"
                ),
                "contributor_count": contributors,
                "minimum_contributors": minimum,
                "value_counts": (
                    dict(sorted(Counter(values).items()))
                    if contributors >= minimum else {}
                ),
            }
        for name in LIST_FIELDS:
            lists = [item[name] for item in annotations if item[name]]
            counts = Counter(value for values in lists for value in values)
            contributors = len(lists)
            fields[name] = {
                "evidence_kind": (
                    "human_preference" if contributors >= minimum else "unavailable"
                ),
                "contributor_count": contributors,
                "minimum_contributors": minimum,
                "shared_values": (
                    [
                        {"value": value, "reference_count": count}
                        for value, count in sorted(counts.items())
                        if count >= minimum
                    ] if contributors >= minimum else []
                ),
            }
        fields["reviewer_notes"] = {
            "evidence_kind": "not_aggregated",
            "contributor_count": sum(
                bool(item["reviewer_notes"]) for item in annotations
            ),
            "reason": "Free-form reviewer notes remain reference-local.",
        }
        return {
            "annotated_reference_count": len(annotations),
            "minimum_contributors": minimum,
            "fields": fields,
        }

    def _with_staleness(self, document: dict[str, Any]) -> dict[str, Any]:
        result = deepcopy(document)
        reasons = []
        inputs = document.get("input_versions")
        if not isinstance(inputs, list):
            raise ReferenceProfileError("Profile input versions are malformed.")
        current = {
            entry["reference_id"]: entry
            for entry in self.library.list_references(
                status="accepted", profile_name=document["profile_name"]
            )
        }
        recorded_ids = {item.get("reference_id") for item in inputs if isinstance(item, dict)}
        if recorded_ids != set(current):
            reasons.append("Accepted reference membership changed after profile build.")
        for item in inputs:
            if not isinstance(item, dict) or not isinstance(item.get("reference_id"), str):
                raise ReferenceProfileError("Profile input versions are malformed.")
            reference_id = item["reference_id"]
            entry = current.get(reference_id)
            if entry is None:
                continue
            try:
                analysis = json.loads(Path(entry["analysis_path"]).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                reasons.append(f"{reference_id} analysis is unavailable: {type(error).__name__}.")
            else:
                if self._analysis_revision(analysis) != item.get("analysis_revision"):
                    reasons.append(f"{reference_id} analysis revision changed.")
            annotation_revision = self.annotation_store.read(reference_id)["revision"]
            if annotation_revision != item.get("annotation_revision"):
                reasons.append(f"{reference_id} annotation revision changed.")
        result["staleness"] = {
            "status": "stale" if reasons else "current",
            "reasons": reasons,
        }
        return result
