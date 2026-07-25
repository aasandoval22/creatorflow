import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from backend.services.video_manifest import (
    DEFAULT_MANIFEST_PATH,
    ClipAnalysisStatus,
    TranscriptionStatus,
    VideoManifest,
    VideoStatus,
    utc_now,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CANDIDATE_DIRECTORY = PROJECT_ROOT / "data" / "clip_candidates"
WORD_RE = re.compile(r"\b[\w']+\b")
EXPLANATIONS = (
    "the reason",
    "here's why",
    "what i like",
    "the problem",
    "the best",
    "you should",
    "what you want",
)
SPONSOR = (
    "sponsor",
    "sponsored",
    "creator code",
    "promo code",
    "discount code",
    "thanks to",
    "brought to you by",
    "supported by",
    "partnered with",
    "affiliate link",
    "use code",
)
CTA = (
    "like and subscribe",
    "subscribe",
    "leave a comment",
    "comment below",
    "smash the like",
)
INTRO = (
    "welcome back",
    "welcome to",
    "hey everyone",
    "hey guys",
    "what's up",
    "my channel",
)
OUTRO = ("thanks for watching", "see you next time", "until next time")
FILLERS = {"um", "uh", "erm", "like", "you know", "basically", "actually"}
SELF_CONTAINED_OPENERS = (
    "how ",
    "why ",
    "the problem",
    "the reason",
    "what i like",
    "the best",
    "here is",
    "here's",
    "you should",
    "if you",
    "this build",
    "this setup",
)
CONTINUATION_OPENERS = (
    "and ",
    "but ",
    "so ",
    "because ",
    "which ",
    "that ",
    "it ",
    "this is where",
    "does not ",
    "made ",
    "range ",
    "grenade ",
    "that's fine",
    "to the next level",
    "guns and then",
    "just the usual",
    "made last year",
    "does not consume",
    "range to work with",
    "this build is that",
)
INCOMPLETE_ENDINGS = (
    "and",
    "but",
    "or",
    "so",
    "because",
    "which",
    "that",
    "if",
    "when",
    "while",
    "to",
    "on",
    "of",
    "in",
    "from",
    "into",
    "about",
    "depending on",
    "with",
    "in fact",
    "for example",
    "the reason is",
    "here's why",
)
SUBJECT_PRONOUNS = {"i", "you", "he", "she", "we", "they", "it", "this", "that"}
CONCRETE_TERMS = {
    "build", "setup", "cooldown", "damage", "weapon", "armor", "grenade",
    "range", "banner", "mechanic", "perk", "skill", "seconds", "percent",
}
MEANINGFUL_PAUSE_SECONDS = 0.65
INVALID_ENDING_PATTERNS = (
    r"\bi['’]?m running$",
    r"\bbecause you should be grappling$",
    r"\bthe higher the difficulty\b.*\byou(?:'re| are) in$",
    r"\btwo[- ]piece crodas$",
    r"\bput on some good$",
)


class TranscriptError(ValueError):
    """Raised when a transcript artifact is unsafe to analyze."""


class AnalysisResultStatus(str, Enum):
    SUCCESS = "success"
    SKIPPED = "skipped"
    FAILED = "failed"


class EndingClassification(str, Enum):
    STRONG_COMPLETE = "strong_complete"
    ACCEPTABLE_COMPLETE_WITHOUT_PUNCTUATION = (
        "acceptable_complete_without_punctuation"
    )
    UNCERTAIN = "uncertain"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class EndingAssessment:
    classification: EndingClassification
    score: float
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class WordEntry:
    text: str
    normalized_text: str
    start: float
    end: float
    source_segment_id: int | str
    source_segment_index: int
    word_index: int


@dataclass(frozen=True)
class AnalysisResult:
    video_id: str
    status: AnalysisResultStatus
    message: str
    candidates_json_path: str | None = None
    candidates: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class AnalysisBatchResult:
    results: list[AnalysisResult]

    @property
    def successful(self) -> int:
        return sum(r.status is AnalysisResultStatus.SUCCESS for r in self.results)

    @property
    def skipped(self) -> int:
        return sum(r.status is AnalysisResultStatus.SKIPPED for r in self.results)

    @property
    def failed(self) -> int:
        return sum(r.status is AnalysisResultStatus.FAILED for r in self.results)


@dataclass(frozen=True)
class CandidateConfiguration:
    minimum_duration_seconds: float = 20
    target_duration_seconds: float = 35
    maximum_duration_seconds: float = 60
    minimum_word_count: int = 35
    maximum_overlap: float = 0.5
    maximum_candidates: int = 10
    padding_before_seconds: float = 0.15
    padding_after_seconds: float = 0.25
    minimum_boundary_confidence: float = 0.55
    meaningful_pause_seconds: float = MEANINGFUL_PAUSE_SECONDS

    def __post_init__(self) -> None:
        if not (
            0 < self.minimum_duration_seconds
            <= self.target_duration_seconds
            <= self.maximum_duration_seconds
        ):
            raise ValueError(
                "Durations must be positive and ordered minimum <= target <= maximum."
            )
        if self.minimum_word_count < 1 or self.maximum_candidates < 1:
            raise ValueError("Word count and maximum candidates must be positive.")
        if not 0 <= self.maximum_overlap <= 1:
            raise ValueError("Maximum overlap must be between 0 and 1.")
        if self.padding_before_seconds < 0 or self.padding_after_seconds < 0:
            raise ValueError("Media padding cannot be negative.")
        if not 0 <= self.minimum_boundary_confidence <= 1:
            raise ValueError("Minimum boundary confidence must be between 0 and 1.")
        if self.meaningful_pause_seconds < 0:
            raise ValueError("Meaningful pause cannot be negative.")


class ClipCandidateGenerator:
    """Generate deterministic heuristic clip rankings from local transcripts."""

    def __init__(
        self,
        manifest_path: Path = DEFAULT_MANIFEST_PATH,
        output_directory: Path = DEFAULT_CANDIDATE_DIRECTORY,
        *,
        manifest: VideoManifest | None = None,
        configuration: CandidateConfiguration | None = None,
    ) -> None:
        self.manifest = manifest or VideoManifest(manifest_path)
        self.output_directory = Path(output_directory)
        self.configuration = configuration or CandidateConfiguration()

    def analyze(
        self,
        *,
        video_id: str | None = None,
        limit: int | None = None,
        force: bool = False,
    ) -> AnalysisBatchResult:
        records = self.manifest.read_records()
        if video_id is not None:
            records = [r for r in records if r["video_id"] == video_id]
        selected = []
        results = []
        for record in records:
            reason = self._ineligible_reason(record)
            if reason:
                results.append(
                    AnalysisResult(
                        record["video_id"], AnalysisResultStatus.SKIPPED, reason
                    )
                )
                continue
            selected.append(record)
        if limit is not None:
            selected = selected[:limit]
        for record in selected:
            if (
                record["clip_analysis"]["status"]
                == ClipAnalysisStatus.COMPLETED.value
                and not force
            ):
                results.append(
                    AnalysisResult(
                        record["video_id"],
                        AnalysisResultStatus.SKIPPED,
                        "Clip analysis is already completed.",
                    )
                )
                continue
            results.append(self._analyze_record(record))
        return AnalysisBatchResult(results)

    @staticmethod
    def _ineligible_reason(record: dict[str, Any]) -> str | None:
        if record["status"] != VideoStatus.DOWNLOADED.value:
            return "Video is not downloaded."
        if (
            record["transcription"]["status"]
            != TranscriptionStatus.COMPLETED.value
        ):
            return "Transcription is not completed."
        path = record["transcription"]["transcript_json_path"]
        if not isinstance(path, str) or not path.strip():
            return "Completed transcription has no transcript JSON path."
        return None

    def _analyze_record(self, record: dict[str, Any]) -> AnalysisResult:
        video_id = record["video_id"]
        self.manifest.update_clip_analysis(
            video_id,
            status=ClipAnalysisStatus.PROCESSING.value,
            started_at=utc_now(),
            completed_at=None,
            candidate_count=0,
            candidates_json_path=None,
            error_message=None,
        )
        try:
            transcript_path = Path(
                record["transcription"]["transcript_json_path"]
            )
            transcript = self.load_transcript(transcript_path, video_id)
            candidates = self.generate_candidates(
                video_id,
                transcript["segments"],
                media_duration=transcript.get("duration_seconds")
                or record.get("duration_seconds"),
            )
            artifact_path = self._write_artifact(
                record, transcript_path, candidates
            )
            self.manifest.update_clip_analysis(
                video_id,
                status=ClipAnalysisStatus.COMPLETED.value,
                completed_at=utc_now(),
                candidate_count=len(candidates),
                candidates_json_path=str(artifact_path),
                error_message=None,
            )
            return AnalysisResult(
                video_id,
                AnalysisResultStatus.SUCCESS,
                f"Created {len(candidates)} ranked clip candidates.",
                str(artifact_path),
                tuple(candidates),
            )
        except Exception as error:
            message = f"Clip analysis failed: {error}"
            self.manifest.update_clip_analysis(
                video_id,
                status=ClipAnalysisStatus.FAILED.value,
                completed_at=None,
                candidate_count=0,
                candidates_json_path=None,
                error_message=message,
            )
            return AnalysisResult(video_id, AnalysisResultStatus.FAILED, message)

    @staticmethod
    def load_transcript(path: Path, video_id: str | None = None) -> dict[str, Any]:
        if not path.is_file():
            raise TranscriptError(f"Transcript JSON file does not exist: {path}")
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise TranscriptError(f"Invalid transcript JSON in {path}: {error}") from error
        if not isinstance(document, dict) or document.get("version") != 1:
            raise TranscriptError("Transcript must be a version 1 JSON object.")
        if video_id is not None and document.get("video_id") != video_id:
            raise TranscriptError("Transcript video_id does not match the manifest.")
        segments = document.get("segments")
        if not isinstance(segments, list) or not segments:
            raise TranscriptError("Transcript segments must be a non-empty list.")
        normalized = []
        for index, segment in enumerate(segments):
            if not isinstance(segment, dict):
                raise TranscriptError(f"Transcript segment {index} must be an object.")
            start, end, text = (
                segment.get("start"),
                segment.get("end"),
                segment.get("text"),
            )
            if (
                isinstance(start, bool)
                or isinstance(end, bool)
                or not isinstance(start, (int, float))
                or not isinstance(end, (int, float))
                or start < 0
                or end <= start
            ):
                raise TranscriptError(
                    f"Transcript segment {index} has invalid timestamps."
                )
            if not isinstance(text, str):
                raise TranscriptError(f"Transcript segment {index} text must be a string.")
            segment_id = segment.get("id", index)
            if segment_id is None:
                segment_id = index
            if isinstance(segment_id, bool) or not isinstance(segment_id, (int, str)):
                raise TranscriptError(f"Transcript segment {index} has an invalid id.")
            normalized_segment = {
                "id": segment_id,
                "start": float(start),
                "end": float(end),
                "text": text.strip(),
            }
            if isinstance(segment.get("words"), list):
                normalized_segment["words"] = segment["words"]
            normalized.append(normalized_segment)
        for previous, current in zip(normalized, normalized[1:]):
            if current["start"] < previous["start"]:
                raise TranscriptError("Transcript segments must be timestamp ordered.")
        return {**document, "segments": normalized}

    def generate_candidates(
        self,
        video_id: str,
        segments: list[dict[str, Any]],
        *,
        media_duration: float | None = None,
    ) -> list[dict[str, Any]]:
        timeline = self.build_word_timeline(segments)
        if timeline:
            return self._generate_word_candidates(
                video_id, timeline, media_duration=media_duration
            )
        return self._generate_segment_candidates(video_id, segments)

    @staticmethod
    def build_word_timeline(
        segments: list[dict[str, Any]],
    ) -> list[WordEntry]:
        """Return valid, chronological Whisper words or an empty fallback signal.

        Malformed, untimed, or nonchronological word records are ignored. If a
        segment has no usable timed words, an empty list explicitly signals the
        caller to use segment-boundary generation instead of bridging unknown
        speech.
        """
        timeline: list[WordEntry] = []
        for segment_index, segment in enumerate(segments):
            raw_words = segment.get("words")
            if not isinstance(raw_words, list) or not raw_words:
                return []
            segment_words: list[WordEntry] = []
            for word_index, raw_word in enumerate(raw_words):
                if not isinstance(raw_word, dict):
                    continue
                text = raw_word.get("word")
                start, end = raw_word.get("start"), raw_word.get("end")
                if (
                    not isinstance(text, str)
                    or not text.strip()
                    or isinstance(start, bool)
                    or isinstance(end, bool)
                    or not isinstance(start, (int, float))
                    or not isinstance(end, (int, float))
                    or start < 0
                    or end <= start
                    or start < segment["start"] - 0.5
                    or end > segment["end"] + 0.5
                ):
                    continue
                normalized_text = " ".join(WORD_RE.findall(text.lower()))
                if not normalized_text:
                    continue
                entry = WordEntry(
                    text=text,
                    normalized_text=normalized_text,
                    start=float(start),
                    end=float(end),
                    source_segment_id=segment["id"],
                    source_segment_index=segment_index,
                    word_index=word_index,
                )
                if segment_words and entry.start < segment_words[-1].end - 0.05:
                    continue
                if timeline and entry.start < timeline[-1].end - 0.05:
                    continue
                segment_words.append(entry)
            if not segment_words:
                return []
            timeline.extend(segment_words)
        return timeline

    def _generate_word_candidates(
        self,
        video_id: str,
        words: list[WordEntry],
        *,
        media_duration: float | None,
    ) -> list[dict[str, Any]]:
        config = self.configuration
        raw: list[dict[str, Any]] = []
        for start_index in range(len(words)):
            start_details, start_reasons = self._assess_word_start(words, start_index)
            if start_details["confidence"] < config.minimum_boundary_confidence:
                continue
            choices: list[dict[str, Any]] = []
            for end_index in range(start_index, len(words)):
                content_duration = words[end_index].end - words[start_index].start
                if content_duration > config.maximum_duration_seconds:
                    break
                if (
                    content_duration < config.minimum_duration_seconds
                    or end_index - start_index + 1 < config.minimum_word_count
                ):
                    continue
                ending, end_details = self._assess_word_end(words, end_index)
                if end_details["confidence"] < config.minimum_boundary_confidence:
                    continue
                choices.append(
                    self._make_word_candidate(
                        video_id,
                        words,
                        start_index,
                        end_index,
                        start_details,
                        end_details,
                        start_reasons,
                        ending,
                        media_duration=media_duration,
                    )
                )
            if choices:
                # Completeness outranks hitting the target exactly.
                choices.sort(
                    key=lambda candidate: (
                        -min(
                            candidate["boundary_details"]["start_confidence"],
                            candidate["boundary_details"]["end_confidence"],
                        ),
                        -candidate["boundary_details"]["end_confidence"],
                        -candidate["component_scores"]["structure_score"],
                        -candidate["score"],
                        abs(
                            candidate["duration"]
                            - config.target_duration_seconds
                        ),
                        candidate["end"],
                    )
                )
                raw.append(choices[0])
        return self._filter_and_rank(raw)

    def _make_word_candidate(
        self,
        video_id: str,
        words: list[WordEntry],
        start_index: int,
        end_index: int,
        start_details: dict[str, Any],
        end_details: dict[str, Any],
        start_reasons: list[str],
        ending: EndingAssessment,
        *,
        media_duration: float | None,
    ) -> dict[str, Any]:
        selected = words[start_index : end_index + 1]
        text = self._join_word_text(selected)
        content_start, content_end = selected[0].start, selected[-1].end
        upper_bound = (
            float(media_duration)
            if isinstance(media_duration, (int, float))
            and not isinstance(media_duration, bool)
            and media_duration > 0
            else words[-1].end
        )
        start = max(0.0, content_start - self.configuration.padding_before_seconds)
        end = min(upper_bound, content_end + self.configuration.padding_after_seconds)
        components, reasons = self.score_candidate(
            text,
            content_end - content_start,
            target_duration=self.configuration.target_duration_seconds,
        )
        start_confidence = start_details["confidence"]
        end_confidence = end_details["confidence"]
        components["start_boundary_score"] = 18.0 * start_confidence
        components["end_boundary_score"] = 18.0 * end_confidence
        components["boundary_penalty_score"] = -24.0 * (
            1.0 - min(start_confidence, end_confidence)
        )
        reasons = (
            start_reasons
            + reasons
            + list(ending.reasons)
            + [
                f"Boundary decision: {start_details['method']} start and "
                f"{end_details['method']} ending"
            ]
        )
        serialized_components = {
            name: round(value, 1) for name, value in components.items()
        }
        total = round(sum(serialized_components.values()), 1)
        if total > 100:
            serialized_components["score_cap_adjustment"] = round(100 - total, 1)
        elif total < 0:
            serialized_components["score_floor_adjustment"] = round(-total, 1)
        segment_ids = list(dict.fromkeys(word.source_segment_id for word in selected))
        identity = f"{video_id}:{content_start:.3f}:{content_end:.3f}".encode()
        return {
            "rank": 0,
            "candidate_id": f"{video_id}-{hashlib.sha256(identity).hexdigest()[:12]}",
            "start": round(start, 3),
            "end": round(end, 3),
            "duration": round(end - start, 3),
            "text": text,
            "score": round(sum(serialized_components.values()), 1),
            "reasons": reasons,
            "ending_classification": ending.classification.value,
            "boundary_details": {
                "start_method": start_details["method"],
                "end_method": end_details["method"],
                "start_confidence": round(start_confidence, 2),
                "end_confidence": round(end_confidence, 2),
            },
            "component_scores": serialized_components,
            "segment_ids": segment_ids,
        }

    @staticmethod
    def _join_word_text(words: list[WordEntry]) -> str:
        if any(word.text[:1].isspace() for word in words):
            return "".join(word.text for word in words).strip()
        return " ".join(word.text.strip() for word in words).strip()

    def _assess_word_start(
        self, words: list[WordEntry], index: int
    ) -> tuple[dict[str, Any], list[str]]:
        word = words[index]
        previous = words[index - 1] if index else None
        opening = " ".join(item.normalized_text for item in words[index : index + 7])
        prior_words = " ".join(
            item.normalized_text for item in words[max(0, index - 4) : index]
        )
        invalid = opening.startswith(CONTINUATION_OPENERS) or (
            opening.startswith(("how much ", "how many "))
            and previous is not None
            and not previous.text.rstrip().endswith((".", "!", "?"))
            and word.start - previous.end
            < self.configuration.meaningful_pause_seconds
        )
        if prior_words.endswith(
            ("because", "and", "but", "or", "so", "that", "which", "where")
        ):
            invalid = True
        prior_punctuation = bool(
            previous and previous.text.rstrip().endswith((".", "!", "?"))
        )
        pause = bool(
            previous
            and word.start - previous.end
            >= self.configuration.meaningful_pause_seconds
        )
        opener = opening.startswith(SELF_CONTAINED_OPENERS)
        clear_subject = bool(
            re.match(
                r"(?:the|this|that|these|those|my|your)\s+[\w']+\s+"
                r"(?:is|are|has|have|uses|gives|does|can|will)\b",
                opening,
            )
        )
        reasons: list[str] = []
        if invalid:
            return (
                {"method": "invalid_continuation", "confidence": 0.1},
                ["Penalized because the opening plainly continues an earlier thought"],
            )
        signals: list[str] = []
        confidence = 0.35
        if previous is None:
            confidence = 0.9
            signals.append("transcript_start")
            reasons.append("Begins at the start of the transcript")
        if prior_punctuation:
            confidence += 0.32
            signals.append("sentence_boundary")
            reasons.append("Begins after sentence punctuation")
        if pause:
            confidence += 0.3
            signals.append("pause")
            reasons.append("Begins after a meaningful pause")
        if opener:
            confidence += 0.3
            signals.append("topic_opener")
            reasons.append("Uses a self-contained opening")
        if clear_subject:
            confidence += 0.18
            signals.append("clear_subject")
            reasons.append("Introduces a clear subject")
        first = word.normalized_text
        if first in {"and", "but", "or", "so", "because", "which", "it", "they", "he", "she"}:
            confidence -= 0.35
            reasons.append("Penalized because the opening depends on prior context")
        method = "_and_".join(signals) if signals else "weak_grammatical_start"
        return {"method": method, "confidence": max(0.0, min(1.0, confidence))}, reasons

    def _assess_word_end(
        self, words: list[WordEntry], index: int
    ) -> tuple[EndingAssessment, dict[str, Any]]:
        selected_tail = self._join_word_text(words[max(0, index - 11) : index + 1])
        following = self._join_word_text(words[index + 1 : index + 7]).lower()
        current = words[index]
        next_word = words[index + 1] if index + 1 < len(words) else None
        pause = bool(
            next_word
            and next_word.start - current.end
            >= self.configuration.meaningful_pause_seconds
        )
        punctuated = current.text.rstrip().endswith((".", "!", "?"))
        ending = self._assess_end({"text": selected_tail})
        semantic_tail = selected_tail.lower().rstrip(" .!?,;:")
        explicit_invalid = any(
            re.search(pattern, semantic_tail) for pattern in INVALID_ENDING_PATTERNS
        )
        setup_continues = (
            following.startswith(
                (
                    "because ",
                    "that ",
                    "which ",
                    "and ",
                    "but ",
                    "how much ",
                    "how many ",
                )
            )
            and not punctuated
        )
        if explicit_invalid or setup_continues:
            ending = EndingAssessment(
                EndingClassification.INCOMPLETE,
                0.0,
                ("Penalized because the ending is visibly unfinished",),
            )
        confidence = {
            EndingClassification.INCOMPLETE: 0.12,
            EndingClassification.UNCERTAIN: 0.2,
            EndingClassification.ACCEPTABLE_COMPLETE_WITHOUT_PUNCTUATION: 0.42,
            EndingClassification.STRONG_COMPLETE: 0.42,
        }[ending.classification]
        signals: list[str] = []
        if punctuated and ending.classification is not EndingClassification.INCOMPLETE:
            confidence += 0.43
            signals.append("sentence_punctuation")
        if pause and ending.classification is not EndingClassification.INCOMPLETE:
            confidence += 0.28
            signals.append("pause")
        if next_word is None and ending.classification is not EndingClassification.INCOMPLETE:
            confidence += 0.2
            signals.append("transcript_end")
        next_topic = (
            following.startswith(SELF_CONTAINED_OPENERS)
            and not following.startswith(("how much ", "how many "))
        )
        next_subject = bool(
            re.match(
                r"(?:the|this|that|these|those|my|your)\s+[\w']+\s+"
                r"(?:is|are|has|have|uses|gives|does|can|will)\b",
                following,
            )
        )
        if (
            ending.classification
            is EndingClassification.ACCEPTABLE_COMPLETE_WITHOUT_PUNCTUATION
            and (pause or next_word is None or next_topic or next_subject)
        ):
            confidence += 0.2
            signals.append("complete_clause")
        if next_topic and ending.classification is not EndingClassification.INCOMPLETE:
            confidence += 0.18
            signals.append("next_topic")
        elif next_subject and ending.classification is not EndingClassification.INCOMPLETE:
            confidence += 0.12
            signals.append("next_subject")
        if ending.classification is EndingClassification.STRONG_COMPLETE:
            signals.append("complete_clause")
        method = "_and_".join(signals) if signals else "incomplete_or_uncertain"
        return ending, {
            "method": method,
            "confidence": max(0.0, min(1.0, confidence)),
        }

    def _filter_and_rank(
        self, raw: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        raw.sort(key=self._candidate_sort_key)
        accepted: list[dict[str, Any]] = []
        for candidate in raw:
            duplicate = any(
                self._text_similarity(candidate["text"], existing["text"]) >= 0.72
                for existing in accepted
            )
            overlap = any(
                self._overlap_fraction(candidate, existing)
                > self.configuration.maximum_overlap
                for existing in accepted
            )
            if duplicate or overlap:
                continue
            accepted.append(candidate)
            if len(accepted) >= self.configuration.maximum_candidates:
                break
        accepted.sort(key=self._final_rank_sort_key)
        for rank, candidate in enumerate(accepted, 1):
            candidate["rank"] = rank
        return accepted

    def _generate_segment_candidates(
        self, video_id: str, segments: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        config = self.configuration
        raw = []
        for start_index, first in enumerate(segments):
            previous = segments[start_index - 1] if start_index else None
            start_score, start_reasons = self._assess_start(first, previous)
            words = []
            choices = []
            for end_index in range(start_index, len(segments)):
                last = segments[end_index]
                duration = last["end"] - first["start"]
                if duration > config.maximum_duration_seconds:
                    break
                words.extend(WORD_RE.findall(last["text"]))
                if (
                    duration >= config.minimum_duration_seconds
                    and len(words) >= config.minimum_word_count
                ):
                    window = segments[start_index : end_index + 1]
                    candidate = self._make_candidate(
                        video_id,
                        window,
                        previous_segment=previous,
                        start_assessment=(start_score, start_reasons),
                    )
                    choices.append(candidate)
            if choices:
                # One window per starting point: favor complete thoughts near the
                # target instead of emitting the first window over the minimum.
                choices.sort(
                    key=lambda c: (
                        -(
                            c["component_scores"]["end_boundary_score"]
                            + c["component_scores"]["duration_fit_score"]
                        ),
                        -c["score"],
                        c["end"],
                    )
                )
                raw.append(choices[0])
        raw.sort(key=self._candidate_sort_key)
        accepted = []
        for candidate in raw:
            duplicate = any(
                self._text_similarity(candidate["text"], existing["text"]) >= 0.72
                for existing in accepted
            )
            overlap = any(
                self._overlap_fraction(candidate, existing)
                > config.maximum_overlap
                for existing in accepted
            )
            if duplicate or overlap:
                continue
            accepted.append(candidate)
            if len(accepted) >= config.maximum_candidates:
                break
        accepted.sort(key=self._final_rank_sort_key)
        for rank, candidate in enumerate(accepted, 1):
            candidate["rank"] = rank
        return accepted

    def _make_candidate(
        self,
        video_id: str,
        segments: list[dict[str, Any]],
        *,
        previous_segment: dict[str, Any] | None = None,
        start_assessment: tuple[float, list[str]] | None = None,
    ) -> dict[str, Any]:
        start, end = segments[0]["start"], segments[-1]["end"]
        text = " ".join(s["text"] for s in segments if s["text"]).strip()
        components, reasons = self.score_candidate(
            text,
            end - start,
            segments,
            target_duration=self.configuration.target_duration_seconds,
        )
        start_score, start_reasons = start_assessment or self._assess_start(
            segments[0], previous_segment
        )
        ending = self._assess_end(segments[-1])
        components["start_boundary_score"] = start_score
        components["end_boundary_score"] = ending.score
        reasons = start_reasons + reasons + list(ending.reasons)
        serialized_components = {
            name: round(value, 1) for name, value in components.items()
        }
        serialized_total = round(sum(serialized_components.values()), 1)
        if serialized_total > 100.0:
            serialized_components["score_cap_adjustment"] = round(
                100.0 - serialized_total, 1
            )
        elif serialized_total < 0.0:
            serialized_components["score_floor_adjustment"] = round(
                -serialized_total, 1
            )
        score = round(sum(serialized_components.values()), 1)
        identity = f"{video_id}:{start:.3f}:{end:.3f}".encode()
        return {
            "rank": 0,
            "candidate_id": f"{video_id}-{hashlib.sha256(identity).hexdigest()[:12]}",
            "start": round(start, 3),
            "end": round(end, 3),
            "duration": round(end - start, 3),
            "text": text,
            "score": score,
            "reasons": reasons,
            "ending_classification": ending.classification.value,
            "boundary_details": {
                "start_method": self._boundary_method(start_reasons, "segment_start"),
                "end_method": self._boundary_method(
                    list(ending.reasons), "segment_end"
                ),
                "start_confidence": round(start_score / 18.0, 2),
                "end_confidence": round(ending.score / 14.0, 2),
            },
            "component_scores": serialized_components,
            "segment_ids": [s["id"] for s in segments],
        }

    @staticmethod
    def _boundary_method(reasons: list[str], default: str) -> str:
        lower = " ".join(reasons).lower()
        labels = []
        if "pause" in lower:
            labels.append("pause")
        if "punctuation" in lower or "completed sentence" in lower:
            labels.append("sentence_boundary")
        if "self-contained" in lower:
            labels.append("topic_opener")
        if "complete" in lower:
            labels.append("complete_clause")
        if "incomplete" in lower or "continues" in lower:
            labels.append("invalid_continuation")
        return "_and_".join(labels) or default

    @staticmethod
    def score_candidate(
        text: str,
        duration: float,
        segments: list[dict[str, Any]] | None = None,
        *,
        target_duration: float = 35,
    ) -> tuple[dict[str, float], list[str]]:
        lower = text.lower()
        words = WORD_RE.findall(lower)
        reasons = []
        hook = 2.0
        first_sentence = re.split(r"(?<=[.!?])\s+", lower, maxsplit=1)[0]
        if lower.startswith(("why ", "how ", "what ")):
            hook += 5.5
            if "?" in first_sentence and len(re.split(r"(?<=[.!?])\s+", text)) > 1:
                reasons.append("Begins with a direct question and immediately answers it")
            else:
                reasons.append("Begins with a direct question")
        if any(phrase in lower for phrase in EXPLANATIONS):
            hook += 3.5
            reasons.append("Provides an explanation or recommendation")
        density = min(9.0, (len(words) / max(duration, 1)) * 3.2)
        excitement = min(6.0, 1.0 + 1.25 * len(re.findall(r"[!?]", text)))
        specificity = min(
            12.0,
            2.0
            + 1.4 * len(set(words) & CONCRETE_TERMS)
            + 2.0 * bool(re.search(r"\b\d+(?:\.\d+)?(?:%| seconds?)?\b", lower)),
        )
        if re.search(r"\b\d+(?:\.\d+)?\b", text):
            reasons.append("Includes a concrete number or detail")
        elif len(set(words) & CONCRETE_TERMS) >= 2:
            reasons.append("Names specific mechanics or equipment")
        clarity = 7.0
        filler_count = sum(word in FILLERS for word in words)
        filler_ratio = filler_count / max(len(words), 1)
        if filler_ratio < 0.04:
            clarity += 2.5
            reasons.append("Uses clear, low-filler language")
        context = 9.0
        opening_words = words[:10]
        pronoun_ratio = sum(word in SUBJECT_PRONOUNS for word in opening_words) / max(
            1, len(opening_words)
        )
        if pronoun_ratio >= 0.3:
            context -= 5.5
            reasons.append("Penalized because the opening is pronoun-heavy")
        elif any(term in first_sentence for term in CONCRETE_TERMS):
            context += 3.0
            reasons.append("Introduces the topic without requiring prior context")
        duration_fit = max(
            0.0, 12.0 * (1.0 - abs(duration - target_duration) / max(target_duration, 1))
        )
        if abs(duration - target_duration) <= max(3.0, target_duration * 0.12):
            reasons.append("Duration is close to the configured target")
        structure = 1.0
        if "?" in first_sentence and len(re.split(r"[.!?]+", text)) >= 2:
            structure += 3.0
        if "problem" in lower and any(word in lower for word in ("solution", "fix", "instead")):
            structure += 3.5
            reasons.append("Presents a problem followed by a solution")
        if any(word in lower for word in ("should", "recommend")) and "because" in lower:
            structure += 2.5
            reasons.append("Supports an actionable recommendation with an explanation")
        if any(f" {word} " in f" {lower} " for word in ("but", "however", "instead")):
            structure += 1.5
            reasons.append("Includes a self-contained contrast")
        penalty = 0.0
        concrete_count = len(set(words) & CONCRETE_TERMS)
        has_payoff = any(
            phrase in lower
            for phrase in (
                "because",
                "the reason",
                "you should",
                "recommend",
                "so that",
                "therefore",
                "takeaway",
            )
        )
        if concrete_count >= 3 and not has_payoff and "?" not in text:
            penalty -= 5.0
            reasons.append(
                "Penalized because it lists equipment or mechanics without a takeaway"
            )
        penalties = (
            (
                SPONSOR,
                -28,
                "Penalized because it contains sponsor or advertisement language",
            ),
            (CTA, -18, "Penalized because it contains a call to action"),
            (
                INTRO,
                -15,
                "Penalized because it contains greeting or channel introduction language",
            ),
            (OUTRO, -18, "Penalized because it contains outro language"),
        )
        for phrases, value, reason in penalties:
            if any(phrase in lower for phrase in phrases):
                penalty += value
                reasons.append(reason)
        if filler_ratio >= 0.08:
            value = -min(15.0, filler_ratio * 100)
            penalty += value
            reasons.append("Penalized because it contains excessive filler words")
        sentences = [
            sentence.strip().lower()
            for sentence in re.split(r"[.!?]+", text)
            if sentence.strip()
        ]
        if len(sentences) != len(set(sentences)):
            penalty -= 8
            reasons.append("Penalized because it repeats substantially duplicate statements")
        components = {
            "hook_score": min(11.0, hook),
            "information_density_score": max(0.0, density),
            "excitement_score": excitement,
            "clarity_score": min(10.0, clarity),
            "specificity_score": specificity,
            "context_independence_score": max(0.0, min(12.0, context)),
            "duration_fit_score": duration_fit,
            "structure_score": min(10.0, structure),
            "penalty_score": penalty,
        }
        return components, reasons

    @staticmethod
    def _assess_start(
        segment: dict[str, Any], previous: dict[str, Any] | None
    ) -> tuple[float, list[str]]:
        text = segment["text"].strip()
        lower = text.lower()
        score = 7.0
        reasons: list[str] = []
        pause = (
            previous is not None and segment["start"] - previous["end"] >= 0.75
        )
        prior_complete = previous is None or previous["text"].rstrip().endswith(
            (".", "!", "?")
        )
        if prior_complete:
            score += 3.0
            reasons.append("Begins after a completed sentence")
        if pause:
            score += 3.0
            reasons.append("Begins after a meaningful pause")
        if lower.startswith(SELF_CONTAINED_OPENERS):
            score += 5.0
            reasons.append("Uses a self-contained opening")
        if lower.startswith(CONTINUATION_OPENERS):
            score -= 8.0
            reasons.append("Penalized because the opening continues a previous thought")
        if lower.startswith("we have ") and previous is not None and not prior_complete:
            score -= 6.0
            reasons.append("Penalized because the opening starts mid-list")
        first_words = WORD_RE.findall(lower)[:8]
        if first_words and first_words[0] in SUBJECT_PRONOUNS and not lower.startswith(
            ("you should", "this build", "this setup")
        ):
            score -= 2.5
        return max(0.0, min(18.0, score)), reasons

    @staticmethod
    def _assess_end(segment: dict[str, Any]) -> EndingAssessment:
        text = segment["text"].strip()
        lower = text.lower().rstrip(" ,;:")
        semantic_end = lower.rstrip(" .!?")
        punctuated = text.endswith((".", "!", "?"))
        incomplete = any(
            semantic_end == ending or semantic_end.endswith(f" {ending}")
            for ending in INCOMPLETE_ENDINGS
        ) or any(re.search(pattern, semantic_end) for pattern in INVALID_ENDING_PATTERNS)
        introducing = bool(
            re.search(
                r"\b(?:first|next|another|the reason is|here's why)[,:]?\s*$",
                semantic_end,
            )
        )
        dependent_clause = semantic_end.startswith(
            ("because ", "which ", "although ", "unless ", "while ", "when ")
        )
        trailing_separator = text.endswith((",", ";", ":"))
        inferred_complete = (
            not punctuated
            and not incomplete
            and not introducing
            and not dependent_clause
            and not trailing_separator
            and len(WORD_RE.findall(semantic_end)) >= 6
            and not semantic_end.startswith(
                ("and ", "but ", "so ", "because ", "which ")
            )
        )
        if incomplete or introducing or dependent_clause or trailing_separator:
            return EndingAssessment(
                EndingClassification.INCOMPLETE,
                0.0,
                ("Penalized because the ending is incomplete",),
            )
        if punctuated:
            return EndingAssessment(
                EndingClassification.STRONG_COMPLETE,
                14.0,
                ("Ends with a complete takeaway",),
            )
        if inferred_complete:
            return EndingAssessment(
                EndingClassification.ACCEPTABLE_COMPLETE_WITHOUT_PUNCTUATION,
                9.0,
                ("Ends with a complete statement despite missing punctuation",),
            )
        return EndingAssessment(EndingClassification.UNCERTAIN, 4.0, ())

    @staticmethod
    def _candidate_sort_key(candidate: dict[str, Any]) -> tuple[float, ...]:
        components = candidate["component_scores"]
        boundary = candidate.get("boundary_details", {})
        start_confidence = boundary.get(
            "start_confidence", components["start_boundary_score"] / 18.0
        )
        end_confidence = boundary.get(
            "end_confidence", components["end_boundary_score"] / 14.0
        )
        return (
            -min(start_confidence, end_confidence),
            -start_confidence,
            -end_confidence,
            -components.get("structure_score", 0.0),
            -candidate["score"],
            -components["duration_fit_score"],
            candidate["start"],
            candidate["end"],
        )

    @staticmethod
    def _final_rank_sort_key(candidate: dict[str, Any]) -> tuple[Any, ...]:
        components = candidate["component_scores"]
        boundary = candidate.get("boundary_details", {})
        return (
            -candidate["score"],
            -boundary.get(
                "start_confidence", components["start_boundary_score"] / 18.0
            ),
            -boundary.get(
                "end_confidence", components["end_boundary_score"] / 14.0
            ),
            -components["duration_fit_score"],
            candidate["start"],
            candidate["candidate_id"],
        )

    @staticmethod
    def _overlap_fraction(a: dict[str, Any], b: dict[str, Any]) -> float:
        overlap = max(0.0, min(a["end"], b["end"]) - max(a["start"], b["start"]))
        return overlap / min(a["duration"], b["duration"])

    @staticmethod
    def _text_similarity(a: str, b: str) -> float:
        left_words = WORD_RE.findall(a.lower())
        right_words = WORD_RE.findall(b.lower())
        left, right = set(left_words), set(right_words)
        containment = len(left & right) / max(1, min(len(left), len(right)))
        left_bigrams = set(zip(left_words, left_words[1:]))
        right_bigrams = set(zip(right_words, right_words[1:]))
        passage = len(left_bigrams & right_bigrams) / max(
            1, min(len(left_bigrams), len(right_bigrams))
        )
        return max(containment, passage)

    def _write_artifact(
        self,
        record: dict[str, Any],
        transcript_path: Path,
        candidates: list[dict[str, Any]],
    ) -> Path:
        directory = self.output_directory / record["video_id"]
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / "candidates.json"
        config = self.configuration
        document = {
            "version": 1,
            "video_id": record["video_id"],
            "source_transcript_path": str(transcript_path),
            "source_media_path": record["local_file_path"],
            "created_at": utc_now(),
            "configuration": {
                "minimum_duration_seconds": config.minimum_duration_seconds,
                "target_duration_seconds": config.target_duration_seconds,
                "maximum_duration_seconds": config.maximum_duration_seconds,
                "maximum_candidates": config.maximum_candidates,
                "padding_before_seconds": config.padding_before_seconds,
                "padding_after_seconds": config.padding_after_seconds,
                "minimum_boundary_confidence": config.minimum_boundary_confidence,
                "meaningful_pause_seconds": config.meaningful_pause_seconds,
            },
            "candidates": candidates,
        }
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=directory,
                prefix=".candidates.json.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                json.dump(document, temporary, indent=2)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, destination)
            return destination
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
