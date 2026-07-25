"""Transparent, evidence-based preview comparison against a local profile."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from backend.services.reference_clip_library import PROJECT_ROOT, _atomic_json
from backend.services.reference_profile_builder import ReferenceProfileBuilder
from backend.services.video_manifest import utc_now

DEFAULT_COMPARISON_ROOT = PROJECT_ROOT / "data" / "reference_comparisons"


class ReferenceComparisonError(ValueError):
    pass


def _finding(status: str, evidence: str, *, kind: str = "known", **values: Any) -> dict[str, Any]:
    return {"status": status, **values, "evidence_kind": kind, "evidence": evidence}


class ReferenceClipComparator:
    def __init__(
        self, profile_builder: ReferenceProfileBuilder,
        output_directory: Path = DEFAULT_COMPARISON_ROOT,
    ) -> None:
        self.profile_builder = profile_builder
        self.output_directory = Path(output_directory)

    def report_path(self, profile_name: str, review_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", review_id):
            raise ReferenceComparisonError("Review identity is invalid.")
        return self.output_directory / profile_name / f"{review_id}.json"

    def compare(
        self, profile_name: str, review: dict[str, Any], *,
        metadata: dict[str, Any] | None = None, transcript: dict[str, Any] | None = None,
        write: bool = False,
    ) -> dict[str, Any]:
        profile = self.profile_builder.read(profile_name)
        metadata = metadata or self._metadata(review)
        transcript = transcript or self._transcript(metadata)
        start = self._number(review.get("render_start", metadata.get("render_start")))
        end = self._number(review.get("render_end", metadata.get("render_end")))
        candidate_start = self._number(review.get("candidate_start", metadata.get("candidate_start")))
        candidate_end = self._number(review.get("candidate_end", metadata.get("candidate_end")))
        duration = self._number(review.get("render_duration", metadata.get("render_duration")))
        if duration is None and start is not None and end is not None:
            duration = end - start
        minimum = profile["duration"]["recommended_minimum"]
        maximum = profile["duration"]["recommended_maximum"]
        if duration is None:
            duration_fit = _finding("unavailable", "Rendered duration is unavailable.", kind="unavailable")
        else:
            status = "within_profile" if minimum <= duration <= maximum else (
                "too_short" if duration < minimum else "too_long"
            )
            duration_fit = _finding(
                status, f"Rendered duration is {duration:.3f}s; the soft profile range is "
                f"{minimum:g}–{maximum:g}s.", observed=round(duration, 3),
                recommended_range=[minimum, maximum],
            )
        words = self._words(transcript)
        window_words = [
            word for word in words if start is not None and end is not None
            and word["end"] > start and word["start"] < end
        ]
        first_source = words[0]["start"] if words else None
        first_rendered = window_words[0]["start"] if window_words else None
        if start is None or not words:
            opening = _finding(
                "unavailable", "Transcript timing or render start is unavailable.",
                kind="unavailable",
            )
        elif first_source < start and (not window_words or first_rendered - start > 1):
            opening = _finding(
                "possibly_late", f"Speech exists at {first_source:.3f}s before the rendered "
                f"preview starts at {start:.3f}s.", kind="heuristic",
            )
        else:
            delay = max(0.0, (first_rendered or start) - start)
            opening = _finding(
                "supported", f"First included speech begins {delay:.3f}s after preview start.",
                kind="heuristic", observed_seconds=round(delay, 3),
            )
        payoff = self._payoff(window_words)
        tail = self._tail(window_words, end)
        layout = self._layout(metadata, profile)
        media_format = self._media_format(metadata, profile)
        captions = metadata.get("caption_configuration")
        caption_finding = _finding(
            "present" if captions else "unavailable",
            "Preview metadata includes caption configuration." if captions
            else "Caption presence/configuration is absent from preview metadata.",
            kind="known" if captions else "unavailable", configuration=captions,
        )
        if None in (start, end, candidate_start, candidate_end):
            containment = _finding(
                "unavailable", "Candidate and render boundaries are not all available.",
                kind="unavailable",
            )
        else:
            contains = start <= candidate_start and end >= candidate_end
            containment = _finding(
                "contained" if contains else "not_contained",
                f"Render window {start:.3f}–{end:.3f}s "
                f"{'contains' if contains else 'does not contain'} candidate "
                f"{candidate_start:.3f}–{candidate_end:.3f}s.",
                contains_candidate=contains,
            )
        report = {
            "version": 1, "created_at": utc_now(),
            "review_id": review.get("review_id"), "video_id": review.get("video_id"),
            "profile_name": profile_name, "profile_confidence": profile["confidence"],
            "findings": {
                "duration_fit": duration_fit, "opening_context": opening,
                "payoff_completion": payoff, "ending_tail": tail, "layout": layout,
                "captions": caption_finding, "candidate_containment": containment,
                "media_format": media_format,
            },
            "limitations": (
                "Findings compare measurable metadata and transcript heuristics. "
                "They do not objectively measure humor, quality, popularity, or virality."
            ),
        }
        if write:
            review_id = review.get("review_id")
            if not isinstance(review_id, str):
                raise ReferenceComparisonError("A review_id is required to write a report.")
            _atomic_json(self.report_path(profile_name, review_id), report)
        return report

    def compare_reviews(
        self, profile_name: str, reviews: list[dict[str, Any]], *,
        status: str | None = None, video_id: str | None = None, write: bool = True,
    ) -> list[dict[str, Any]]:
        return [
            self.compare(profile_name, review, write=write) for review in reviews
            if (status is None or review.get("status") == status)
            and (video_id is None or review.get("video_id") == video_id)
        ]

    @staticmethod
    def _number(value: Any) -> float | None:
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    @staticmethod
    def _metadata(review: dict[str, Any]) -> dict[str, Any]:
        path = review.get("preview_metadata_path")
        if not isinstance(path, str):
            return {}
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _transcript(metadata: dict[str, Any]) -> dict[str, Any]:
        path = metadata.get("source_transcript_path")
        if not isinstance(path, str):
            return {}
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    @classmethod
    def _words(cls, transcript: dict[str, Any]) -> list[dict[str, Any]]:
        result = []
        for segment in transcript.get("segments", []):
            candidates = segment.get("words") or []
            if not candidates and all(key in segment for key in ("start", "end", "text")):
                candidates = [segment]
            for word in candidates:
                start, end = cls._number(word.get("start")), cls._number(word.get("end"))
                text = str(word.get("word", word.get("text", ""))).strip()
                if start is not None and end is not None and end >= start and text:
                    result.append({"start": start, "end": end, "text": text})
        return sorted(result, key=lambda word: word["start"])

    @staticmethod
    def _payoff(words: list[dict[str, Any]]) -> dict[str, Any]:
        if not words:
            return _finding("unavailable", "No word-level transcript evidence is available.",
                            kind="unavailable")
        text = " ".join(word["text"] for word in words)
        questions = [index for index, word in enumerate(words) if "?" in word["text"]]
        answer_terms = ("because", "yes", "no", "answer", "got it", "there it is",
                        "actually", "that's why", "finally")
        if questions:
            last_question = questions[-1]
            later = " ".join(word["text"] for word in words[last_question + 1:]).casefold()
            if not later or not any(term in later for term in answer_terms):
                return _finding(
                    "unresolved", "The final transcript contains a question with no later "
                    "answer/payoff-language signal.", kind="heuristic",
                )
            return _finding(
                "likely_complete", "Answer/payoff-language occurs after the final question.",
                kind="heuristic",
            )
        abrupt = not re.search(r"[.!?][\"']?$", text.strip())
        if abrupt:
            return _finding(
                "possibly_incomplete", "The included transcript ends without sentence-ending "
                "punctuation.", kind="heuristic",
            )
        return _finding(
            "no_unresolved_signal", "The included transcript ends at a sentence boundary and "
            "contains no unresolved final question.", kind="heuristic",
        )

    @staticmethod
    def _tail(words: list[dict[str, Any]], render_end: float | None) -> dict[str, Any]:
        if not words or render_end is None:
            return _finding("unavailable", "Final speech or render-end timing is unavailable.",
                            kind="unavailable")
        tail = max(0.0, render_end - words[-1]["end"])
        status = "short" if tail < 0.5 else ("excessive" if tail > 5 else "reasonable")
        return _finding(
            status, f"The preview continues {tail:.3f}s after the final included speech.",
            kind="heuristic", observed_seconds=round(tail, 3),
        )

    @staticmethod
    def _layout(metadata: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
        observed = metadata.get("layout") or metadata.get("composition")
        if isinstance(observed, dict):
            observed = observed.get("composition")
        reference = profile["layout"]["preferred_composition"]
        if not isinstance(observed, str):
            # Existing renderer metadata identifies its deterministic filter layout implicitly.
            configuration = metadata.get("render_configuration", metadata.get("configuration", {}))
            if isinstance(configuration, dict) and configuration.get("width") and configuration.get("height"):
                observed = "centered_landscape_with_blurred_background"
        if not observed:
            return _finding("unavailable", "Layout metadata is unavailable.", kind="unavailable",
                            reference=reference)
        return _finding(
            "matching" if observed == reference else "different",
            f"Observed layout is {observed}; reference preference is {reference}.",
            observed=observed, reference=reference,
        )

    @staticmethod
    def _media_format(metadata: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
        probe = metadata.get("probe", {})
        config = metadata.get("render_configuration", {})
        width = probe.get("width") if isinstance(probe, dict) else None
        height = probe.get("height") if isinstance(probe, dict) else None
        frame_rate = config.get("frame_rate") if isinstance(config, dict) else None
        observed = f"{width}x{height}" if width and height else None
        references = profile.get("media", {}).get("observed_resolutions", [])
        if observed is None and frame_rate is None:
            return _finding(
                "unavailable", "Resolution and frame-rate metadata are unavailable.",
                kind="unavailable", reference_resolutions=references,
            )
        status = "matching_resolution" if observed in references else "different_or_unavailable_resolution"
        return _finding(
            status, f"Rendered media reports resolution {observed or 'unavailable'} and "
            f"frame rate {frame_rate if frame_rate is not None else 'unavailable'} fps; "
            f"reference resolutions are {', '.join(references) or 'unavailable'}.",
            observed_resolution=observed, observed_frame_rate=frame_rate,
            reference_resolutions=references,
            reference_frame_rate=profile.get("media", {}).get("observed_frame_rate_median"),
        )
