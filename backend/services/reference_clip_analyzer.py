"""Deterministic local media, timing, and transcript analysis for references."""

from __future__ import annotations

import json
import math
import re
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Sequence

from backend.services.reference_clip_library import (
    ReferenceClipError, ReferenceClipLibrary, _atomic_json,
    annotation_defaults, load_and_validate_baseline, sha256_file,
)
from backend.services.video_manifest import utc_now


class ReferenceAnalysisError(ValueError):
    pass


Runner = Callable[[Sequence[str]], Any]


def _default_runner(command: Sequence[str]) -> Any:
    # File-backed streams avoid pipe backpressure and unbounded live buffering.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        completed = subprocess.run(
            list(command), stdout=stdout, stderr=stderr, check=False,
        )
        stdout.seek(0)
        stderr.seek(0)
        return SimpleNamespace(
            returncode=completed.returncode,
            stdout=stdout.read().decode("utf-8", errors="replace"),
            stderr=stderr.read().decode("utf-8", errors="replace"),
        )


def parse_scene_changes(output: str) -> list[float]:
    values = re.findall(r"(?:pts_time:|lavfi\.scene_score=.*?pts_time=)(\d+(?:\.\d+)?)", output)
    return sorted({round(float(value), 6) for value in values})


def parse_silence_intervals(output: str) -> list[dict[str, float]]:
    starts = [float(value) for value in re.findall(r"silence_start:\s*(\d+(?:\.\d+)?)", output)]
    ends = [
        (float(end), float(duration))
        for end, duration in re.findall(
            r"silence_end:\s*(\d+(?:\.\d+)?)\s*\|\s*silence_duration:\s*(\d+(?:\.\d+)?)",
            output,
        )
    ]
    intervals = []
    for index, (end, duration) in enumerate(ends):
        start = starts[index] if index < len(starts) else max(0.0, end - duration)
        intervals.append({"start": round(start, 6), "end": round(end, 6),
                          "duration": round(end - start, 6)})
    return intervals


class ReferenceClipAnalyzer:
    def __init__(
        self, library: ReferenceClipLibrary, *, ffprobe_path: str = "ffprobe",
        ffmpeg_path: str = "ffmpeg", runner: Runner | None = None,
        transcriber: Any | None = None, scene_threshold: float = 0.3,
        model_name: str = "base.en", device: str = "cpu",
        compute_type: str = "int8", language: str = "en",
    ) -> None:
        self.library = library
        self.ffprobe_path = ffprobe_path
        self.ffmpeg_path = ffmpeg_path
        self.runner = runner or _default_runner
        self.transcriber = transcriber
        self.scene_threshold = scene_threshold
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self.language = language

    def analyze(
        self, reference_id: str, *, transcription: bool = True, force: bool = False
    ) -> dict[str, Any]:
        entry = self.library.get(reference_id)
        output_path = self.library.paths.resolve(entry["analysis_path"])
        existing: dict[str, Any] | None = None
        if output_path.is_file() and not force:
            try:
                existing = json.loads(output_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise ReferenceAnalysisError(
                    f"Existing analysis {output_path} is unreadable; use --force after inspecting it."
                ) from error
            if existing.get("reference_id") != reference_id:
                raise ReferenceAnalysisError("Existing analysis belongs to another reference.")
            return existing
        if output_path.is_file() and force:
            try:
                value = json.loads(output_path.read_text(encoding="utf-8"))
                if isinstance(value, dict) and value.get("reference_id") == reference_id:
                    existing = value
            except (OSError, json.JSONDecodeError):
                existing = None
        try:
            self.library.validate_checksum(reference_id)
            media_path = self.library.paths.resolve(
                entry["media_path"], must_exist=True, regular=True
            )
            baseline = load_and_validate_baseline(
                self.library.paths.resolve(
                    entry["baseline_path"], must_exist=True, regular=True
                )
            )
            probe = self._probe(media_path)
            scenes = self._scene_changes(media_path)
            silences = self._silences(media_path)
            transcript = (
                self._transcribe(media_path)
                if transcription
                else {"language": None, "words": []}
            )
            speech = self._speech_metrics(transcript, probe["duration"])
            now = utc_now()
            previous_revision = self._analysis_revision(existing)
            document = {
                "version": 2,
                "reference_id": reference_id,
                "analysis_revision": previous_revision + 1,
                "created_at": (
                    existing.get("created_at")
                    if existing and isinstance(existing.get("created_at"), str)
                    else now
                ),
                "updated_at": now,
                "media": {
                    **probe, "file_size_bytes": media_path.stat().st_size,
                    "checksum_sha256": sha256_file(media_path),
                },
                "transcription": {
                    "requested": transcription,
                    "status": "available" if transcription else "disabled",
                    "language": transcript.get("language"),
                    "word_timestamps": transcription,
                    "evidence_kind": (
                        "transcript_heuristic" if transcription else "unavailable"
                    ),
                },
                "speech": speech,
                "visual_timing": {
                    "scene_change_timestamps": scenes,
                    "scene_change_threshold": self.scene_threshold,
                    "evidence_kind": "pixel_change_signal",
                },
                "audio_timing": {"silence_intervals": silences},
                "annotations": annotation_defaults(baseline),
                "limitations": [
                    "Scene changes are pixel-change signals, not proof of topic changes.",
                    "Transcript signals are heuristics, not proof of humor, quality, or virality.",
                    "No visual semantic claims are made.",
                ],
            }
            _atomic_json(output_path, document)
            return document
        except ReferenceAnalysisError:
            raise
        except Exception as error:
            raise ReferenceAnalysisError(f"Analysis failed for {reference_id}: {error}") from error

    @staticmethod
    def _analysis_revision(document: dict[str, Any] | None) -> int:
        if document is None:
            return 0
        value = document.get("analysis_revision", 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ReferenceAnalysisError("Existing analysis revision is invalid.")
        return value

    def _run(self, command: Sequence[str], label: str) -> Any:
        result = self.runner(command)
        code = getattr(result, "returncode", 0)
        if code:
            detail = str(getattr(result, "stderr", "")).strip()
            raise ReferenceAnalysisError(f"{label} failed (exit {code}): {detail or 'no diagnostic output'}")
        return result

    def _probe(self, path: Path) -> dict[str, Any]:
        result = self._run([
            self.ffprobe_path, "-v", "error", "-show_streams", "-show_format",
            "-of", "json", str(path),
        ], "FFprobe")
        try:
            document = json.loads(str(result.stdout))
        except json.JSONDecodeError as error:
            raise ReferenceAnalysisError(f"FFprobe returned malformed JSON: {error}") from error
        streams = document.get("streams")
        if not isinstance(streams, list):
            raise ReferenceAnalysisError("FFprobe output has no streams list.")
        video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
        if not isinstance(video, dict):
            raise ReferenceAnalysisError("Reference media has no video stream.")
        audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
        duration = document.get("format", {}).get("duration", video.get("duration"))
        try:
            duration_value = float(duration)
            width, height = int(video["width"]), int(video["height"])
        except (KeyError, TypeError, ValueError) as error:
            raise ReferenceAnalysisError("FFprobe output lacks valid duration or dimensions.") from error
        if not math.isfinite(duration_value) or duration_value <= 0 or width <= 0 or height <= 0:
            raise ReferenceAnalysisError("FFprobe returned invalid media measurements.")
        average = self._rate(video.get("avg_frame_rate"))
        rate = self._rate(video.get("r_frame_rate")) or average
        return {
            "duration": round(duration_value, 6), "width": width, "height": height,
            "display_aspect_ratio": video.get("display_aspect_ratio") or f"{width}:{height}",
            "frame_rate": rate, "average_frame_rate": average,
            "video_codec": str(video.get("codec_name") or "unknown"),
            "audio_codec": str(audio.get("codec_name")) if audio else None,
            "audio_present": audio is not None,
            "sample_rate": int(audio["sample_rate"]) if audio and str(audio.get("sample_rate", "")).isdigit() else None,
        }

    @staticmethod
    def _rate(value: Any) -> float | None:
        if not isinstance(value, str) or "/" not in value:
            return None
        numerator, denominator = value.split("/", 1)
        try:
            result = float(numerator) / float(denominator)
        except (ValueError, ZeroDivisionError):
            return None
        return round(result, 6) if math.isfinite(result) else None

    def _scene_changes(self, path: Path) -> list[float]:
        result = self._run([
            self.ffmpeg_path, "-hide_banner", "-nostdin", "-i", str(path),
            "-vf", f"select='gt(scene,{self.scene_threshold})',showinfo",
            "-an", "-f", "null", "-",
        ], "FFmpeg scene detection")
        return parse_scene_changes(f"{getattr(result, 'stdout', '')}\n{getattr(result, 'stderr', '')}")

    def _silences(self, path: Path) -> list[dict[str, float]]:
        result = self._run([
            self.ffmpeg_path, "-hide_banner", "-nostdin", "-i", str(path),
            "-af", "silencedetect=noise=-35dB:d=0.35", "-vn", "-f", "null", "-",
        ], "FFmpeg silence detection")
        return parse_silence_intervals(f"{getattr(result, 'stdout', '')}\n{getattr(result, 'stderr', '')}")

    def _transcribe(self, path: Path) -> dict[str, Any]:
        if self.transcriber is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as error:
                raise ReferenceAnalysisError(
                    "Transcription requested but faster-whisper is unavailable. "
                    "Install the optional transcription requirements or use --no-transcription."
                ) from error
            self.transcriber = WhisperModel(
                self.model_name, device=self.device, compute_type=self.compute_type
            )
        result = self.transcriber.transcribe(
            str(path), word_timestamps=True, vad_filter=True,
            language=self.language, beam_size=5
        )
        segments, info = result
        words = []
        for segment in segments:
            for word in getattr(segment, "words", None) or []:
                text = str(getattr(word, "word", "") or "").strip()
                start, end = getattr(word, "start", None), getattr(word, "end", None)
                if text and isinstance(start, (int, float)) and isinstance(end, (int, float)):
                    words.append({"word": text, "start": float(start), "end": float(end)})
        return {"language": getattr(info, "language", None) or self.language, "words": words}

    @staticmethod
    def _speech_metrics(transcript: dict[str, Any], duration: float) -> dict[str, Any]:
        words = sorted(transcript.get("words", []), key=lambda word: word["start"])
        texts = [str(word["word"]).strip() for word in words]
        full_text = " ".join(texts)
        first = words[0]["start"] if words else None
        last = words[-1]["end"] if words else None
        pauses = [
            {"start": round(left["end"], 3), "end": round(right["start"], 3),
             "duration": round(right["start"] - left["end"], 3)}
            for left, right in zip(words, words[1:])
            if right["start"] - left["end"] >= 0.75
        ]
        questions = [
            {"word_index": index, "text": text}
            for index, text in enumerate(texts) if "?" in text
        ]
        reaction_terms = ("wow", "what", "no way", "oh my", "bro", "dude", "let's go", "haha", "lol")
        payoff_terms = ("because", "actually", "got it", "there it is", "that's why", "finally", "answer")
        lower = full_text.casefold()
        reactions = [term for term in reaction_terms if term in lower]
        payoffs = [term for term in payoff_terms if term in lower]
        if "!" in full_text:
            reactions.append("exclamation")
        if re.search(r"\b(ha){2,}\b", lower):
            reactions.append("laughter_language")
        spoken_duration = (
            max(0.0, float(last) - float(first))
            if first is not None and last is not None else None
        )
        voiced_seconds = sum(
            max(0.0, float(word["end"]) - float(word["start"]))
            for word in words
        )
        early_text = " ".join(texts[:12]).casefold()
        hook_signals = []
        if words and float(first) <= 3.0:
            hook_signals.append("speech_within_first_3_seconds")
        if "?" in " ".join(texts[:12]):
            hook_signals.append("early_question")
        if "!" in " ".join(texts[:12]):
            hook_signals.append("early_exclamation")
        if any(term in early_text for term in reaction_terms):
            hook_signals.append("early_reaction_language")
        likely_hook = {
            "status": "heuristic_signal" if hook_signals else (
                "speech_start_only" if words else "unavailable"
            ),
            "timestamp": round(float(first), 3) if first is not None else None,
            "signals": hook_signals,
            "evidence_kind": "transcript_heuristic" if words else "unavailable",
            "evidence": (
                "Early transcript timing or language suggests a possible hook."
                if hook_signals else
                "Speech timing is available, but no hook-language signal was detected."
                if words else "No word-level transcript evidence is available."
            ),
        }
        payoff_word_indexes = []
        for index in range(len(texts)):
            window = " ".join(texts[max(0, index - 3):index + 1]).casefold()
            if any(term in window for term in payoff_terms):
                payoff_word_indexes.append(index)
        if reactions and words:
            for index, text in enumerate(texts):
                if "!" in text and index >= len(texts) // 2:
                    payoff_word_indexes.append(index)
        payoff_index = max(payoff_word_indexes) if payoff_word_indexes else None
        payoff_time = (
            float(words[payoff_index]["end"])
            if payoff_index is not None else None
        )
        likely_payoff = {
            "status": "heuristic_signal" if payoff_time is not None else "unavailable",
            "timestamp": round(payoff_time, 3) if payoff_time is not None else None,
            "signals": payoffs + (["late_exclamation"] if payoff_time is not None and reactions else []),
            "evidence_kind": (
                "transcript_heuristic" if payoff_time is not None else "unavailable"
            ),
            "evidence": (
                "Transcript language or a late exclamation suggests a possible payoff."
                if payoff_time is not None else
                "No transcript-language payoff signal was detected."
            ),
        }
        unresolved = []
        stripped = full_text.rstrip()
        if stripped.endswith("?"):
            unresolved.append("ending_question")
        if stripped and not re.search(r"[.!?][\"']?$", stripped):
            unresolved.append("no_terminal_sentence_boundary")
        last_question = max(
            (index for index, text in enumerate(texts) if "?" in text),
            default=None,
        )
        if last_question is not None and (
            payoff_index is None or last_question > payoff_index
        ):
            unresolved.append("question_without_later_payoff_signal")
        return {
            "language": transcript.get("language"), "words": words,
            "word_count": len(words),
            "words_per_second": round(len(words) / duration, 4) if duration else 0,
            "words_per_spoken_second": (
                round(len(words) / spoken_duration, 4)
                if spoken_duration and spoken_duration > 0 else None
            ),
            "spoken_duration": round(spoken_duration, 3) if spoken_duration is not None else None,
            "speech_density": round(voiced_seconds / duration, 4) if duration else 0,
            "first_word_start": round(first, 3) if first is not None else None,
            "initial_speech_delay": round(first, 3) if first is not None else None,
            "last_word_end": round(last, 3) if last is not None else None,
            "post_speech_tail": round(max(0.0, duration - last), 3) if last is not None else None,
            "meaningful_pauses": pauses, "sentence_boundaries": [
                index for index, text in enumerate(texts) if re.search(r"[.!?]$", text)
            ],
            "questions": questions, "reaction_signals": reactions,
            "payoff_signals": payoffs,
            "transcript_excerpt": full_text[:500],
            "likely_hook": likely_hook,
            "likely_payoff": likely_payoff,
            "unresolved_ending_indicators": unresolved,
            "post_payoff_tail": (
                round(max(0.0, duration - payoff_time), 3)
                if payoff_time is not None else None
            ),
            "signal_evidence_kind": "transcript_heuristic",
        }
