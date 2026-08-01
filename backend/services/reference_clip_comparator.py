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
        write: bool = False, profile_document: dict[str, Any] | None = None,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        profile = profile_document or self.profile_builder.read(profile_name)
        if profile.get("profile_name") != profile_name:
            raise ReferenceComparisonError("Pinned profile identity does not match.")
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
        timing_findings = self._timing_findings(
            profile, window_words, start=start, end=end, duration=duration,
            payoff_finding=payoff,
        )
        scene_finding = self._signal_count(
            metadata.get("scene_change_timestamps"), profile, "scene_changes",
            "pixel-change scene signals",
        )
        silence_finding = self._signal_count(
            metadata.get("silence_intervals"), profile, "silence_intervals",
            "detected silence intervals",
        )
        human = self._human_preferences(metadata, profile)
        report = {
            "version": 2, "created_at": created_at or utc_now(),
            "review_id": review.get("review_id"), "video_id": review.get("video_id"),
            "profile_name": profile_name, "profile_confidence": profile["confidence"],
            "findings": {
                "duration_fit": duration_fit, "opening_context": opening,
                "payoff_completion": payoff, "ending_tail": tail, "layout": layout,
                "captions": caption_finding, "candidate_containment": containment,
                "media_format": media_format,
                **timing_findings,
                "scene_activity": scene_finding,
                "silence_activity": silence_finding,
                "human_preferences": human,
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

    def _timing_findings(
        self, profile: dict[str, Any], words: list[dict[str, Any]], *,
        start: float | None, end: float | None, duration: float | None,
        payoff_finding: dict[str, Any],
    ) -> dict[str, Any]:
        automatic = profile.get("automatic_evidence", {})
        if start is None or end is None or duration is None or not words:
            unavailable = lambda label: _finding(
                "unavailable", f"{label} is unavailable without word-level transcript timing.",
                kind="unavailable",
            )
            return {
                "speech_density": unavailable("Speech density"),
                "spoken_pacing": unavailable("Spoken pacing"),
                "speech_start": unavailable("Speech-start timing"),
                "hook_timing": unavailable("Hook timing"),
                "payoff_timing": unavailable("Payoff timing"),
                "post_speech_tail": unavailable("Post-speech tail"),
                "post_payoff_tail": unavailable("Post-payoff tail"),
                "unresolved_ending": unavailable("Unresolved-ending evidence"),
            }
        voiced = sum(max(0.0, min(end, word["end"]) - max(start, word["start"]))
                     for word in words)
        spoken_span = max(word["end"] for word in words) - min(word["start"] for word in words)
        density = voiced / duration if duration > 0 else None
        pacing = len(words) / spoken_span if spoken_span > 0 else None
        first = max(0.0, words[0]["start"] - start)
        last = max(0.0, end - words[-1]["end"])
        payoff_terms = ("because", "actually", "got it", "there it is", "that's why",
                        "finally", "answer")
        payoff_words = [word for index, word in enumerate(words)
                        if any(term in " ".join(
                            value["text"] for value in words[max(0, index - 3):index + 1]
                        ).casefold() for term in payoff_terms)]
        payoff_time = payoff_words[-1]["end"] - start if payoff_words else None
        post_payoff = end - payoff_words[-1]["end"] if payoff_words else None
        return {
            "speech_density": self._metric_finding(density, automatic.get("speech_density"),
                "speech density", kind="heuristic"),
            "spoken_pacing": self._metric_finding(pacing,
                automatic.get("words_per_spoken_second"), "words per spoken second",
                kind="heuristic"),
            "speech_start": self._metric_finding(first, automatic.get("speech_start"),
                "speech-start timing", kind="known"),
            "hook_timing": self._metric_finding(first, automatic.get("hook_timing"),
                "likely hook timing", kind="heuristic"),
            "payoff_timing": self._metric_finding(payoff_time,
                automatic.get("payoff_timing"), "likely payoff timing", kind="heuristic"),
            "post_speech_tail": self._metric_finding(last,
                automatic.get("post_speech_tail"), "post-speech tail", kind="heuristic"),
            "post_payoff_tail": self._metric_finding(post_payoff,
                automatic.get("post_payoff_tail"), "post-payoff tail", kind="heuristic"),
            "unresolved_ending": _finding(
                "signal_present" if payoff_finding["status"] in {"unresolved", "possibly_incomplete"}
                else "no_signal",
                f"Transcript ending heuristic is {payoff_finding['status']}; this is not a quality judgment.",
                kind="heuristic",
            ),
        }

    @staticmethod
    def _metric_finding(
        observed: float | None, aggregate: Any, label: str, *, kind: str,
    ) -> dict[str, Any]:
        if observed is None:
            return _finding("unavailable", f"Observed {label} is unavailable.", kind="unavailable")
        reference_range = aggregate.get("range") if isinstance(aggregate, dict) else None
        if not (isinstance(reference_range, list) and len(reference_range) == 2
                and all(isinstance(value, (int, float)) for value in reference_range)):
            return _finding(
                "observed_profile_unavailable",
                f"Observed {label} is {observed:.4f}; the pinned profile has no comparable range.",
                kind=kind, observed=round(observed, 4), reference_range=None,
            )
        status = "within_observed_range" if reference_range[0] <= observed <= reference_range[1] else "different"
        return _finding(
            status,
            f"Observed {label} is {observed:.4f}; the reference observed range is "
            f"{reference_range[0]:g}–{reference_range[1]:g}. A difference is not a defect.",
            kind=kind, observed=round(observed, 4), reference_range=reference_range,
        )

    def _signal_count(
        self, observed: Any, profile: dict[str, Any], key: str, label: str,
    ) -> dict[str, Any]:
        if not isinstance(observed, list):
            return _finding("unavailable", f"Preview {label} are unavailable.", kind="unavailable")
        return self._metric_finding(
            float(len(observed)), profile.get("automatic_evidence", {}).get(key),
            label, kind="known",
        )

    @staticmethod
    def _human_preferences(metadata: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
        observed = metadata.get("human_annotations")
        fields = profile.get("human_preferences", {}).get("fields", {})
        available = {
            name: value.get("summary") for name, value in fields.items()
            if name in {"opening_style", "clip_purpose", "pacing", "payoff_type"}
            and isinstance(value, dict) and value.get("summary", {}).get("status") != "unavailable"
        }
        if not available:
            return _finding(
                "unavailable", "The profile has insufficient human preference contributors.",
                kind="unavailable",
            )
        if not isinstance(observed, dict):
            return _finding(
                "profile_only", "Human preferences exist in the profile, but the preview has no "
                "human annotations to compare. Preferences are not automatic defects.",
                kind="human", profile_preferences=available,
            )
        return _finding(
            "compared", "Preview annotations are shown beside human profile preferences; "
            "differences remain acceptable review evidence.", kind="human",
            observed=observed, profile_preferences=available,
        )

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
