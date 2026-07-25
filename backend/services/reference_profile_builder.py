"""Build deterministic soft-prior profiles from accepted local references."""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

from backend.services.reference_clip_library import (
    PROJECT_ROOT, ReferenceClipError, ReferenceClipLibrary, _atomic_json,
    load_and_validate_baseline,
)

DEFAULT_PROFILE_ROOT = PROJECT_ROOT / "data" / "reference_profiles"


class ReferenceProfileError(ValueError):
    pass


class ReferenceProfileBuilder:
    def __init__(
        self, library: ReferenceClipLibrary, output_directory: Path = DEFAULT_PROFILE_ROOT
    ) -> None:
        self.library = library
        self.output_directory = Path(output_directory)

    def profile_path(self, profile_name: str) -> Path:
        if "/" in profile_name or "\\" in profile_name or profile_name in {"", ".", ".."}:
            raise ReferenceProfileError("Profile name is invalid.")
        return self.output_directory / f"{profile_name}.json"

    def build(self, profile_name: str) -> dict[str, Any]:
        references = self.library.list_references(status="accepted", profile_name=profile_name)
        if not references:
            raise ReferenceProfileError(f"No accepted references are registered for {profile_name!r}.")
        durations, baselines, ids, analyses = [], [], [], []
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
        ids.sort()
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
        document = {
            "version": 1, "profile_name": profile_name, "reference_ids": ids,
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
            "limitations": [
                "This profile is a deterministic soft prior, not an AI training sample.",
                "Duration observations do not prescribe one duration for future clips.",
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
            not isinstance(document, dict) or document.get("version") != 1
            or document.get("profile_name") != profile_name
        ):
            raise ReferenceProfileError(f"Profile {path} is malformed.")
        return document
