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


class TranscriptError(ValueError):
    """Raised when a transcript artifact is unsafe to analyze."""


class AnalysisResultStatus(str, Enum):
    SUCCESS = "success"
    SKIPPED = "skipped"
    FAILED = "failed"


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
                video_id, transcript["segments"]
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
            normalized.append(
                {"id": segment_id, "start": float(start), "end": float(end), "text": text.strip()}
            )
        for previous, current in zip(normalized, normalized[1:]):
            if current["start"] < previous["start"]:
                raise TranscriptError("Transcript segments must be timestamp ordered.")
        return {**document, "segments": normalized}

    def generate_candidates(
        self, video_id: str, segments: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        config = self.configuration
        raw = []
        for start_index, first in enumerate(segments):
            words = []
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
                    pause_before = (
                        start_index > 0
                        and first["start"] - segments[start_index - 1]["end"]
                        >= 0.75
                    )
                    raw.append(
                        self._make_candidate(
                            video_id, window, boundary_start=pause_before
                        )
                    )
                    if duration >= config.target_duration_seconds:
                        break
        raw.sort(key=lambda c: (-c["score"], c["start"], c["end"]))
        accepted = []
        seen_text = []
        for candidate in raw:
            duplicate = any(
                self._text_similarity(candidate["text"], text) >= 0.85
                for text in seen_text
            )
            overlap = any(
                self._overlap_fraction(candidate, existing)
                > config.maximum_overlap
                for existing in accepted
            )
            if duplicate or overlap:
                continue
            accepted.append(candidate)
            seen_text.append(candidate["text"])
            if len(accepted) >= config.maximum_candidates:
                break
        for rank, candidate in enumerate(accepted, 1):
            candidate["rank"] = rank
        return accepted

    def _make_candidate(
        self,
        video_id: str,
        segments: list[dict[str, Any]],
        *,
        boundary_start: bool = False,
    ) -> dict[str, Any]:
        start, end = segments[0]["start"], segments[-1]["end"]
        text = " ".join(s["text"] for s in segments if s["text"]).strip()
        components, reasons = self.score_candidate(
            text, end - start, segments
        )
        if boundary_start:
            components["standalone_score"] = min(
                18, components["standalone_score"] + 2
            )
            reasons.append("Starts after a meaningful pause")
        score = max(0.0, min(100.0, sum(components.values())))
        identity = f"{video_id}:{start:.3f}:{end:.3f}".encode()
        return {
            "rank": 0,
            "candidate_id": f"{video_id}-{hashlib.sha256(identity).hexdigest()[:12]}",
            "start": round(start, 3),
            "end": round(end, 3),
            "duration": round(end - start, 3),
            "text": text,
            "score": round(score, 1),
            "component_scores": components,
            "reasons": reasons,
            "segment_ids": [s["id"] for s in segments],
        }

    @staticmethod
    def score_candidate(
        text: str, duration: float, segments: list[dict[str, Any]] | None = None
    ) -> tuple[dict[str, int], list[str]]:
        lower = text.lower()
        words = WORD_RE.findall(lower)
        reasons = []
        hook = 5
        if "?" in text or lower.startswith(("why ", "how ", "what ")):
            hook += 5
            reasons.append("Opens with or contains a question")
        if any(phrase in lower for phrase in EXPLANATIONS):
            hook += 6
            reasons.append("Contains a strong explanation or recommendation")
        standalone = 10
        if lower.startswith(("and ", "but ", "so ", "this ", "that ", "it ")):
            standalone -= 5
            reasons.append("Opening depends on earlier context")
        else:
            standalone += 4
            reasons.append("Opens as a standalone thought")
        density = min(15, round((len(words) / max(duration, 1)) * 7))
        if density >= 11:
            reasons.append("Has high spoken-word density")
        excitement = min(12, 2 + 2 * len(re.findall(r"[!?]", text)))
        if re.search(r"\b\d+(?:\.\d+)?\b", text):
            excitement += 3
            reasons.append("Includes a concrete number or detail")
        clarity = 12
        filler_count = sum(word in FILLERS for word in words)
        filler_ratio = filler_count / max(len(words), 1)
        if filler_ratio < 0.04:
            clarity += 4
            reasons.append("Uses clear, low-filler language")
        completion = 5
        if text.rstrip().endswith((".", "!", "?")):
            completion += 9
            reasons.append("Ends with a complete statement")
        penalty = 0
        penalties = (
            (SPONSOR, -28, "Contains sponsor or advertisement language"),
            (CTA, -18, "Contains a call to action"),
            (INTRO, -15, "Contains greeting or channel introduction language"),
            (OUTRO, -18, "Contains outro language"),
        )
        for phrases, value, reason in penalties:
            if any(phrase in lower for phrase in phrases):
                penalty += value
                reasons.append(reason)
        if filler_ratio >= 0.08:
            value = -min(15, round(filler_ratio * 100))
            penalty += value
            reasons.append("Contains excessive filler words")
        sentences = [
            sentence.strip().lower()
            for sentence in re.split(r"[.!?]+", text)
            if sentence.strip()
        ]
        if len(sentences) != len(set(sentences)):
            penalty -= 8
            reasons.append("Repeats substantially duplicate statements")
        if len(words) > 80 and not text.rstrip().endswith((".", "!", "?")):
            penalty -= 10
            reasons.append("Ends with a very long incomplete statement")
        components = {
            "hook_score": min(20, hook),
            "standalone_score": max(0, min(18, standalone)),
            "information_density_score": max(0, density),
            "excitement_score": min(15, excitement),
            "clarity_score": min(18, clarity),
            "completion_score": min(14, completion),
            "penalty_score": penalty,
        }
        return components, reasons

    @staticmethod
    def _overlap_fraction(a: dict[str, Any], b: dict[str, Any]) -> float:
        overlap = max(0.0, min(a["end"], b["end"]) - max(a["start"], b["start"]))
        return overlap / min(a["duration"], b["duration"])

    @staticmethod
    def _text_similarity(a: str, b: str) -> float:
        left, right = set(WORD_RE.findall(a.lower())), set(WORD_RE.findall(b.lower()))
        return len(left & right) / max(1, len(left | right))

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
