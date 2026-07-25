"""Deterministic transcript-informed expansion of candidate render windows."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True)
class ContextExpansionConfiguration:
    profile: str = "reaction"
    preferred_lead_in: float = 15.0
    preferred_tail: float = 12.0
    minimum_lead_in: float = 10.0
    minimum_tail: float = 8.0
    minimum_final_duration: float = 50.0
    target_final_duration: float = 60.0
    maximum_final_duration: float = 90.0
    start_boundary_search_radius: float = 6.0
    end_boundary_search_radius: float = 10.0
    allow_longer: bool = False

    def __post_init__(self) -> None:
        numeric = (
            "preferred_lead_in", "preferred_tail", "minimum_lead_in",
            "minimum_tail", "minimum_final_duration", "target_final_duration",
            "maximum_final_duration", "start_boundary_search_radius",
            "end_boundary_search_radius",
        )
        for name in numeric:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name.replace('_', ' ').title()} must be numeric.")
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name.replace('_', ' ').title()} must be nonnegative.")
        if self.minimum_lead_in > self.preferred_lead_in:
            raise ValueError("Minimum lead-in must not exceed preferred lead-in.")
        if self.minimum_tail > self.preferred_tail:
            raise ValueError("Minimum tail must not exceed preferred tail.")
        if self.minimum_final_duration > self.target_final_duration:
            raise ValueError("Minimum final duration must not exceed target final duration.")
        if self.target_final_duration > self.maximum_final_duration:
            raise ValueError("Target final duration must not exceed maximum final duration.")

    @classmethod
    def for_profile(cls, profile: str, **overrides: Any) -> "ContextExpansionConfiguration":
        if profile == "reaction":
            config = cls()
        elif profile == "compact":
            config = cls(
                profile="compact", preferred_lead_in=6.0, preferred_tail=6.0,
                minimum_lead_in=0.0, minimum_tail=0.0,
                minimum_final_duration=35.0, target_final_duration=45.0,
                maximum_final_duration=60.0,
            )
        else:
            raise ValueError("Context profile must be reaction or compact.")
        unknown = set(overrides) - set(config.__dataclass_fields__)
        if unknown:
            raise ValueError(f"Unknown context configuration: {sorted(unknown)[0]}.")
        return replace(config, **overrides)


@dataclass(frozen=True)
class ContextExpansionResult:
    candidate_start: float
    candidate_end: float
    render_start: float
    render_end: float
    render_duration: float
    lead_in_seconds: float
    tail_seconds: float
    start_boundary_method: str
    end_boundary_method: str
    start_boundary_confidence: float
    end_boundary_confidence: float
    expansion_reasons: tuple[str, ...]


@dataclass(frozen=True)
class _Unit:
    start: float
    end: float
    text: str


class ClipContextExpander:
    """Treat a transcript candidate as an anchor for a complete render window."""

    PAYOFF = re.compile(
        r"\b(what rank|guess the rank|what do you think|let'?s see|watch this|"
        r"does he|will he|how much|why|what happened|the answer|i think it'?s|"
        r"my guess|let'?s find out)\b", re.I,
    )
    RESULT = re.compile(
        r"\b(answer|result|rank(?:ed)?|because|so (?:it|that)|it(?:'s| is)|"
        r"(?:bronze|silver|gold|platinum|diamond|champion)|\d+(?:\.\d+)?)\b",
        re.I,
    )
    REACTION = re.compile(r"(?:!|\b(?:wow|whoa|oh my|no way|let'?s go|haha|lol|lmao)\b)", re.I)
    NEW_TOPIC = re.compile(r"^\s*(?:anyway|next|moving on|now,? let'?s|all right,? next)\b", re.I)

    def expand(
        self, candidate_start: float, candidate_end: float,
        source_duration: float, transcript: dict[str, Any],
        configuration: ContextExpansionConfiguration,
    ) -> ContextExpansionResult:
        start = self._number(candidate_start, "Candidate start")
        end = self._number(candidate_end, "Candidate end")
        source = self._number(source_duration, "Source duration")
        if start < 0 or end <= start or source <= 0 or end > source + 0.05:
            raise ValueError("Candidate range and source duration are invalid.")
        end = min(end, source)
        candidate_duration = end - start
        if candidate_duration > configuration.maximum_final_duration and not configuration.allow_longer:
            raise ValueError(
                f"Candidate duration {candidate_duration:.3f}s exceeds maximum final "
                f"duration {configuration.maximum_final_duration:.3f}s; use --allow-longer."
            )

        units = self._units(transcript)
        anchor_text = self._text_between(units, start, end)
        unresolved = bool(self.PAYOFF.search(anchor_text))
        reasons = [
            f"Applied {configuration.profile} context profile.",
            f"Requested {configuration.preferred_lead_in:g}s lead-in and "
            f"{configuration.preferred_tail:g}s tail.",
        ]
        if unresolved:
            reasons.append("Anchor contains unresolved question or payoff language.")

        render_start = max(0.0, start - configuration.preferred_lead_in)
        render_end = min(source, end + configuration.preferred_tail)
        render_start, start_method, start_confidence = self._snap_start(
            render_start, start, units, configuration.start_boundary_search_radius
        )
        render_end, end_method, end_confidence, end_reason = self._snap_end(
            render_end, end, source, units,
            configuration.end_boundary_search_radius, unresolved,
        )
        if end_reason:
            reasons.append(end_reason)

        # Guarantee configured minimum tails whenever the source makes that possible.
        render_start = min(render_start, max(0.0, start - configuration.minimum_lead_in))
        render_end = max(render_end, min(source, end + configuration.minimum_tail))

        desired = min(configuration.target_final_duration, source)
        minimum = min(configuration.minimum_final_duration, source)
        if render_end - render_start < minimum:
            missing = minimum - (render_end - render_start)
            # Gameplay/review setup gets first claim, except unresolved payoffs favor tail.
            first_tail = unresolved
            render_start, render_end = self._grow(
                render_start, render_end, source, missing,
                first_tail=first_tail and not end_reason
            )
            reasons.append("Expanded to meet the minimum final duration where source time allowed.")
        if render_end - render_start < desired:
            missing = desired - (render_end - render_start)
            render_start, render_end = self._grow(
                render_start, render_end, source, missing,
                first_tail=unresolved and not end_reason
            )
            reasons.append("Expanded toward the target final duration.")

        maximum = configuration.maximum_final_duration
        if not configuration.allow_longer and render_end - render_start > maximum:
            optional_lead = start - render_start
            optional_tail = render_end - end
            allowance = max(0.0, maximum - candidate_duration)
            total = optional_lead + optional_tail
            lead = allowance * optional_lead / total if total else 0.0
            tail = allowance - lead
            render_start, render_end = start - lead, end + tail
            reasons.append("Reduced optional context proportionally to honor maximum duration.")
        elif configuration.allow_longer and render_end - render_start > maximum:
            reasons.append("Explicit allow-longer override permitted the expanded duration.")

        render_start = round(max(0.0, min(start, render_start)), 3)
        render_end = round(min(source, max(end, render_end)), 3)
        if render_start == 0.0:
            reasons.append("Lead-in was clamped to source start.")
        if abs(render_end - source) < 0.001:
            reasons.append("Tail was clamped to source end.")
        return ContextExpansionResult(
            round(start, 3), round(end, 3), render_start, render_end,
            round(render_end - render_start, 3), round(start - render_start, 3),
            round(render_end - end, 3), start_method, end_method,
            start_confidence, end_confidence, tuple(dict.fromkeys(reasons)),
        )

    @staticmethod
    def _grow(
        start: float, end: float, source: float, amount: float, *, first_tail: bool
    ) -> tuple[float, float]:
        before, after = start, source - end
        first_available, second_available = (after, before) if first_tail else (before, after)
        first = min(amount, first_available)
        second = min(amount - first, second_available)
        if first_tail:
            return start - second, end + first
        return start - first, end + second

    def _snap_start(
        self, proposed: float, candidate_start: float, units: list[_Unit], radius: float
    ) -> tuple[float, str, float]:
        options: list[tuple[float, float, str, float]] = []
        for index, unit in enumerate(units):
            if abs(unit.start - proposed) <= radius and unit.start <= candidate_start:
                prior = units[index - 1] if index else None
                pause = unit.start - prior.end if prior else 0.0
                if pause >= 0.65:
                    options.append((abs(unit.start - proposed), unit.start, "meaningful_pause", 0.9))
                if prior and re.search(r"[.!?…][\"']?$", prior.text.strip()):
                    options.append((abs(unit.start - proposed), unit.start, "sentence_boundary", 0.85))
                if re.match(r"^\s*(?:so|okay|all right|look|here|this|what|why|how)\b", unit.text, re.I):
                    options.append((abs(unit.start - proposed), unit.start, "setup_or_restart", 0.7))
        if not options:
            return proposed, "preferred_lead_in", 0.5
        _, boundary, method, confidence = min(options, key=lambda value: (value[0], value[1]))
        return boundary, method, confidence

    def _snap_end(
        self, proposed: float, candidate_end: float, source: float,
        units: list[_Unit], radius: float, unresolved: bool,
    ) -> tuple[float, str, float, str | None]:
        after = [(index, unit) for index, unit in enumerate(units) if unit.end >= candidate_end]
        payoff_seen = not unresolved
        for index, unit in after:
            if (
                unresolved and unit.start >= candidate_end
                and (self.RESULT.search(unit.text) or self.REACTION.search(unit.text))
            ):
                payoff_seen = True
            next_unit = units[index + 1] if index + 1 < len(units) else None
            pause = (next_unit.start - unit.end) if next_unit else source - unit.end
            complete = bool(re.search(r"[.!?…][\"']?$", unit.text.strip()))
            new_topic = bool(next_unit and self.NEW_TOPIC.search(next_unit.text))
            within = proposed <= unit.end <= proposed + radius
            if payoff_seen and within and complete and (pause >= 0.65 or new_topic):
                method = "new_topic_transition" if new_topic else "complete_sentence_pause"
                reason = (
                    "Continued through a payoff signal before the next topic."
                    if unresolved else "Ended at a complete sentence followed by a meaningful pause."
                )
                return min(source, unit.end), method, 0.9, reason
        # Avoid truncating a transcript unit or visibly unfinished sentence.
        overlapping = next((unit for unit in units if unit.start < proposed < unit.end), None)
        if overlapping:
            return min(source, overlapping.end), "unfinished_sentence_extended", 0.75, (
                "Extended the ending to avoid cutting off an unfinished transcript unit."
            )
        return proposed, "preferred_tail", 0.5, None

    @staticmethod
    def _units(transcript: dict[str, Any]) -> list[_Unit]:
        units: list[_Unit] = []
        segments = transcript.get("segments", []) if isinstance(transcript, dict) else []
        for segment in segments if isinstance(segments, list) else []:
            if not isinstance(segment, dict):
                continue
            words = segment.get("words")
            source = words if isinstance(words, list) and words else [segment]
            parts: list[_Unit] = []
            for item in source:
                if not isinstance(item, dict):
                    continue
                text = item.get("word", item.get("text"))
                start, end = item.get("start"), item.get("end")
                if (
                    isinstance(text, str) and text.strip()
                    and isinstance(start, (int, float)) and not isinstance(start, bool)
                    and isinstance(end, (int, float)) and not isinstance(end, bool)
                    and math.isfinite(start) and math.isfinite(end) and end > start
                ):
                    parts.append(_Unit(float(start), float(end), text.strip()))
            # Segment-sized units preserve punctuation and improve sentence snapping.
            if parts:
                text = segment.get("text")
                units.append(_Unit(
                    parts[0].start, parts[-1].end,
                    text.strip() if isinstance(text, str) and text.strip()
                    else " ".join(part.text for part in parts),
                ))
        return sorted(units, key=lambda unit: (unit.start, unit.end, unit.text))

    @staticmethod
    def _text_between(units: list[_Unit], start: float, end: float) -> str:
        return " ".join(unit.text for unit in units if unit.end > start and unit.start < end)

    @staticmethod
    def _number(value: Any, label: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{label} must be numeric.")
        result = float(value)
        if not math.isfinite(result):
            raise ValueError(f"{label} must be finite.")
        return result
