"""Entirely local vertical preview rendering with burned-in captions."""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Sequence

from backend.services.video_manifest import (
    DEFAULT_MANIFEST_PATH,
    ClipAnalysisStatus,
    TranscriptionStatus,
    VideoManifest,
    VideoStatus,
    utc_now,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PREVIEW_DIRECTORY = PROJECT_ROOT / "data" / "previews"
SAFE_PRESETS = (
    "ultrafast", "superfast", "veryfast", "faster", "fast",
    "medium", "slow", "slower", "veryslow",
)
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class PreviewResultStatus(str, Enum):
    SUCCESS = "success"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class RenderConfiguration:
    width: int = 1080
    height: int = 1920
    frame_rate: float = 30
    video_codec: str = "libx264"
    pixel_format: str = "yuv420p"
    audio_codec: str = "aac"
    audio_bitrate: str = "192k"
    crf: int = 20
    preset: str = "medium"
    captions_enabled: bool = True

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0 or self.width % 2 or self.height % 2:
            raise ValueError("Width and height must be positive even integers.")
        if self.frame_rate <= 0:
            raise ValueError("Frame rate must be positive.")
        if not 0 <= self.crf <= 51:
            raise ValueError("CRF must be from 0 through 51.")
        if self.preset not in SAFE_PRESETS:
            raise ValueError(f"Preset must be one of: {', '.join(SAFE_PRESETS)}.")


@dataclass(frozen=True)
class CaptionConfiguration:
    font_name: str = "DejaVu Sans"
    font_size: int = 62
    maximum_words: int = 6
    maximum_characters: int = 34
    maximum_duration_seconds: float = 2.5
    meaningful_pause_seconds: float = 0.65
    minimum_duration_seconds: float = 0.35

    def __post_init__(self) -> None:
        if not self.font_name.strip():
            raise ValueError("Caption font name must not be empty.")
        if (
            self.font_size <= 0 or self.maximum_words <= 0
            or self.maximum_characters <= 0
            or self.maximum_duration_seconds <= 0
        ):
            raise ValueError("Caption grouping values and font size must be positive.")


@dataclass(frozen=True)
class CaptionEvent:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class MediaProbe:
    duration: float
    width: int
    height: int
    video_codec: str
    audio_codec: str | None

    @property
    def has_audio(self) -> bool:
        return self.audio_codec is not None


@dataclass(frozen=True)
class PreviewResult:
    status: PreviewResultStatus
    message: str
    video_id: str
    candidate_id: str | None = None
    candidate_rank: int | None = None
    start: float | None = None
    end: float | None = None
    duration: float | None = None
    output_path: str | None = None
    metadata_path: str | None = None
    command: tuple[str, ...] = ()
    rendered: bool = False


class PreviewError(ValueError):
    """Raised for actionable preview validation or rendering failures."""


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PreviewError(f"{label} must be numeric.")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise PreviewError(f"{label} must be finite.")
    return parsed


def ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    centiseconds = int(round(seconds * 100))
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    whole, fraction = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{whole:02d}.{fraction:02d}"


def escape_ass_text(text: str) -> str:
    return (
        text.replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("\r", " ")
        .replace("\n", r"\N")
    )


class VideoPreviewRenderer:
    def __init__(
        self,
        manifest_path: Path = DEFAULT_MANIFEST_PATH,
        output_directory: Path = DEFAULT_PREVIEW_DIRECTORY,
        *,
        configuration: RenderConfiguration | None = None,
        caption_configuration: CaptionConfiguration | None = None,
        ffmpeg_path: str = "ffmpeg",
        ffprobe_path: str = "ffprobe",
        command_runner: CommandRunner = subprocess.run,
        executable_finder: Callable[[str], str | None] = shutil.which,
        duration_tolerance: float = 1.0,
    ) -> None:
        self.manifest = VideoManifest(Path(manifest_path))
        self.output_directory = Path(output_directory)
        self.configuration = configuration or RenderConfiguration()
        self.caption_configuration = caption_configuration or CaptionConfiguration()
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path
        self.command_runner = command_runner
        self.executable_finder = executable_finder
        self.duration_tolerance = duration_tolerance

    def render(
        self,
        video_id: str,
        *,
        rank: int | None = None,
        candidate_id: str | None = None,
        candidates_path: Path | None = None,
        force: bool = False,
        dry_run: bool = False,
        output_path: Path | None = None,
    ) -> PreviewResult:
        try:
            context = self.prepare(
                video_id, rank=rank, candidate_id=candidate_id,
                candidates_path=candidates_path,
            )
            candidate = context["candidate"]
            final_video, metadata_path = self._output_paths(
                video_id, candidate["candidate_id"],
                output_path or self.output_directory,
            )
            if not force and self._valid_existing(final_video, metadata_path):
                return self._result(
                    PreviewResultStatus.SKIPPED,
                    "A valid preview already exists; use --force to replace it.",
                    context, final_video, metadata_path,
                )

            events = self.generate_caption_events(
                context["transcript"], candidate["start"], candidate["end"]
            ) if self.configuration.captions_enabled else []
            subtitle_placeholder = Path("<temporary-captions.ass>")
            temporary_video = final_video.with_name(f".{final_video.name}.render.tmp.mp4")
            command = self.build_ffmpeg_command(
                context["media_path"], temporary_video, candidate,
                context["source_probe"].has_audio,
                subtitle_placeholder if self.configuration.captions_enabled else None,
            )
            if dry_run:
                return self._result(
                    PreviewResultStatus.SUCCESS,
                    f"Dry run validated {len(events)} caption event(s); command was not rendered.",
                    context, final_video, metadata_path, command=command, rendered=False,
                )

            self._require_executable(self.ffmpeg_path, "FFmpeg")
            final_video.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".preview.", suffix=".tmp.mp4", dir=final_video.parent
            )
            os.close(descriptor)
            temporary_video = Path(temporary_name)
            temporary_video.unlink()
            subtitle_path: Path | None = None
            try:
                if self.configuration.captions_enabled:
                    subtitle_path = self._write_temporary_ass(
                        events, final_video.parent
                    )
                command = self.build_ffmpeg_command(
                    context["media_path"], temporary_video, candidate,
                    context["source_probe"].has_audio, subtitle_path,
                )
                completed = self._run(command)
                if completed.returncode:
                    excerpt = (completed.stderr or "").strip()[-1200:]
                    raise PreviewError(
                        f"FFmpeg failed with exit code {completed.returncode}: "
                        f"{excerpt or 'no error output'}"
                    )
                if not temporary_video.is_file():
                    raise PreviewError("FFmpeg reported success but created no preview file.")
                output_probe = self.probe_media(temporary_video)
                self._validate_output_probe(
                    output_probe, candidate["duration"],
                    context["source_probe"].has_audio,
                )
                self._publish(
                    temporary_video, final_video, metadata_path,
                    context, output_probe,
                )
            finally:
                for temporary in (temporary_video, subtitle_path):
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
            return self._result(
                PreviewResultStatus.SUCCESS, "Preview rendered and verified.",
                context, final_video, metadata_path, command=command, rendered=True,
            )
        except (OSError, PreviewError, json.JSONDecodeError) as error:
            return PreviewResult(
                PreviewResultStatus.FAILED, str(error), video_id,
            )

    def prepare(
        self,
        video_id: str,
        *,
        rank: int | None = None,
        candidate_id: str | None = None,
        candidates_path: Path | None = None,
    ) -> dict[str, Any]:
        if rank is not None and candidate_id is not None:
            raise PreviewError("Candidate ID and rank are mutually exclusive.")
        if rank is not None and (isinstance(rank, bool) or rank < 1):
            raise PreviewError("Candidate rank must be a positive integer.")
        record = self.manifest.get(video_id)
        if record is None:
            raise PreviewError(f"Video {video_id!r} was not found in the manifest.")
        if record["status"] != VideoStatus.DOWNLOADED.value:
            raise PreviewError(f"Video {video_id!r} must have downloaded status.")
        raw_media = record.get("local_file_path")
        if not isinstance(raw_media, str) or not raw_media.strip():
            raise PreviewError(f"Video {video_id!r} has no valid local media path.")
        media_path = Path(raw_media).expanduser().resolve()
        if not media_path.is_file():
            raise PreviewError(f"Source media file does not exist: {media_path}")
        analysis = record.get("clip_analysis", {})
        if analysis.get("status") != ClipAnalysisStatus.COMPLETED.value:
            raise PreviewError(f"Clip analysis for {video_id!r} is not completed.")
        selected_candidates_path = candidates_path or analysis.get(
            "candidates_json_path"
        )
        if not selected_candidates_path:
            raise PreviewError("The completed clip analysis has no candidate artifact path.")
        selected_candidates_path = Path(selected_candidates_path).expanduser().resolve()
        artifact = self._read_json(selected_candidates_path, "candidate artifact")
        candidate = self.select_candidate(
            artifact, video_id, rank=rank, candidate_id=candidate_id
        )
        transcription = record.get("transcription", {})
        if transcription.get("status") != TranscriptionStatus.COMPLETED.value:
            raise PreviewError(f"Transcription for {video_id!r} is not completed.")
        transcript_path = transcription.get("transcript_json_path")
        if not isinstance(transcript_path, str) or not transcript_path:
            raise PreviewError("The completed transcription has no transcript JSON path.")
        transcript_path = Path(transcript_path).expanduser().resolve()
        transcript = self._read_json(transcript_path, "transcript artifact")
        if transcript.get("version") != 1 or transcript.get("video_id") != video_id:
            raise PreviewError("Transcript artifact version or video ID is invalid.")
        if not isinstance(transcript.get("segments"), list):
            raise PreviewError("Transcript artifact segments must be a list.")
        self._require_executable(self.ffprobe_path, "FFprobe")
        source_probe = self.probe_media(media_path)
        if candidate["end"] > source_probe.duration + 0.05:
            raise PreviewError(
                f"Candidate end {candidate['end']:.3f}s exceeds source duration "
                f"{source_probe.duration:.3f}s."
            )
        return {
            "record": record, "candidate": candidate, "transcript": transcript,
            "media_path": media_path, "transcript_path": transcript_path,
            "candidates_path": selected_candidates_path, "source_probe": source_probe,
        }

    def select_candidate(
        self, artifact: dict[str, Any], video_id: str, *,
        rank: int | None = None, candidate_id: str | None = None,
    ) -> dict[str, Any]:
        if artifact.get("version") != 1:
            raise PreviewError("Candidate artifact version must be 1.")
        if artifact.get("video_id") != video_id:
            raise PreviewError("Candidate artifact video ID does not match the selected video.")
        candidates = artifact.get("candidates")
        if not isinstance(candidates, list):
            raise PreviewError("Candidate artifact candidates must be a list.")
        wanted_rank = 1 if rank is None and candidate_id is None else rank
        validated = [self._validate_candidate(item) for item in candidates]
        selected = next((
            item for item in validated
            if (candidate_id is not None and item["candidate_id"] == candidate_id)
            or (candidate_id is None and item["rank"] == wanted_rank)
        ), None)
        if selected is None:
            selector = f"ID {candidate_id!r}" if candidate_id else f"rank {wanted_rank}"
            raise PreviewError(f"No candidate with {selector} was found.")
        return selected

    @staticmethod
    def _validate_candidate(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise PreviewError("Each candidate must be an object.")
        rank = raw.get("rank")
        candidate_id = raw.get("candidate_id")
        if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
            raise PreviewError("Candidate rank must be a positive integer.")
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            raise PreviewError("Candidate ID must be a non-empty string.")
        if not isinstance(raw.get("text"), str):
            raise PreviewError(f"Candidate {candidate_id!r} text must be a string.")
        start = _number(raw.get("start"), "Candidate start")
        end = _number(raw.get("end"), "Candidate end")
        duration = _number(raw.get("duration"), "Candidate duration")
        if start < 0 or end <= start or duration <= 0:
            raise PreviewError(f"Candidate {candidate_id!r} has an invalid time range.")
        if abs(duration - (end - start)) > 0.1:
            raise PreviewError(
                f"Candidate {candidate_id!r} duration disagrees with end minus start."
            )
        item = dict(raw)
        item.update(start=start, end=end, duration=duration)
        return item

    def probe_media(self, path: Path) -> MediaProbe:
        command = [
            self.ffprobe_path, "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ]
        result = self._run(command)
        if result.returncode:
            raise PreviewError(
                f"FFprobe failed for {path}: {(result.stderr or '').strip() or 'no error output'}"
            )
        try:
            document = json.loads(result.stdout or "")
        except json.JSONDecodeError as error:
            raise PreviewError(f"FFprobe returned malformed JSON for {path}: {error}") from error
        streams = document.get("streams")
        if not isinstance(streams, list):
            raise PreviewError("FFprobe response is missing the stream list.")
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        if video is None:
            raise PreviewError(f"FFprobe found no video stream in {path}.")
        duration = document.get("format", {}).get("duration", video.get("duration"))
        try:
            duration_value = float(duration)
        except (TypeError, ValueError) as error:
            raise PreviewError("FFprobe returned an invalid media duration.") from error
        if not math.isfinite(duration_value) or duration_value <= 0:
            raise PreviewError("FFprobe returned an invalid media duration.")
        width, height = video.get("width"), video.get("height")
        codec = video.get("codec_name")
        if not isinstance(width, int) or width <= 0 or not isinstance(height, int) or height <= 0:
            raise PreviewError("FFprobe returned invalid video dimensions.")
        if not isinstance(codec, str) or not codec:
            raise PreviewError("FFprobe returned no video codec.")
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        audio_codec = audio.get("codec_name") if audio else None
        if audio is not None and not isinstance(audio_codec, str):
            raise PreviewError("FFprobe returned an invalid audio codec.")
        return MediaProbe(duration_value, width, height, codec, audio_codec)

    def generate_caption_events(
        self, transcript: dict[str, Any], candidate_start: float, candidate_end: float
    ) -> list[CaptionEvent]:
        duration = candidate_end - candidate_start
        timed: list[tuple[float, float, str]] = []
        for segment in transcript.get("segments", []):
            if not isinstance(segment, dict):
                continue
            for word in segment.get("words", []) if isinstance(segment.get("words"), list) else []:
                parsed = self._timed_text(word, "word")
                if parsed and parsed[1] > candidate_start and parsed[0] < candidate_end:
                    timed.append(parsed)
        if not timed:
            for segment in transcript.get("segments", []):
                parsed = self._timed_text(segment, "text") if isinstance(segment, dict) else None
                if parsed and parsed[1] > candidate_start and parsed[0] < candidate_end:
                    timed.append(parsed)
        entries = [
            (max(0.0, start - candidate_start), min(duration, end - candidate_start), text)
            for start, end, text in sorted(timed)
            if end > candidate_start and start < candidate_end
        ]
        entries = [entry for entry in entries if entry[1] > entry[0] and entry[2]]
        groups: list[list[tuple[float, float, str]]] = []
        current: list[tuple[float, float, str]] = []
        config = self.caption_configuration
        for entry in entries:
            proposed = current + [entry]
            text = self._join_caption_text(item[2] for item in proposed)
            should_break = bool(current) and (
                len(proposed) > config.maximum_words
                or len(text) > config.maximum_characters * 2
                or entry[1] - current[0][0] > config.maximum_duration_seconds
                or entry[0] - current[-1][1] >= config.meaningful_pause_seconds
                or bool(re.search(r"[.!?…][\"']?$", current[-1][2].strip()))
            )
            if should_break:
                groups.append(current)
                current = [entry]
            else:
                current = proposed
        if current:
            groups.append(current)
        events: list[CaptionEvent] = []
        for group in groups:
            start = max(events[-1].end if events else 0.0, group[0][0])
            end = min(duration, group[-1][1], start + config.maximum_duration_seconds)
            if end <= start:
                continue
            text = self._wrap_caption(
                self._join_caption_text(item[2] for item in group)
            )
            events.append(CaptionEvent(round(start, 3), round(end, 3), text))
        if len(events) > 1 and events[-1].end - events[-1].start < config.minimum_duration_seconds:
            final = events[-1]
            prior = events[-2]
            merged_text = self._join_caption_text((prior.text.replace("\n", " "), final.text))
            if (
                final.end - prior.start <= config.maximum_duration_seconds
                and len(merged_text) <= config.maximum_characters * 2
            ):
                events[-2:] = [CaptionEvent(prior.start, final.end, self._wrap_caption(merged_text))]
        return events

    @staticmethod
    def _timed_text(item: dict[str, Any], key: str) -> tuple[float, float, str] | None:
        text = item.get(key)
        start, end = item.get("start"), item.get("end")
        if (
            not isinstance(text, str) or not text.strip()
            or isinstance(start, bool) or isinstance(end, bool)
            or not isinstance(start, (int, float)) or not isinstance(end, (int, float))
            or not math.isfinite(start) or not math.isfinite(end) or end <= start
        ):
            return None
        return float(start), float(end), text.strip()

    @staticmethod
    def _join_caption_text(parts: Sequence[str] | Any) -> str:
        text = ""
        for part in parts:
            clean = str(part).strip()
            if not clean:
                continue
            if text and re.match(r"^[,.;:!?…)\]}]", clean):
                text += clean
            elif text:
                text += " " + clean
            else:
                text = clean
        return text

    def _wrap_caption(self, text: str) -> str:
        maximum = self.caption_configuration.maximum_characters
        words = text.split()
        if len(text) <= maximum or len(words) < 2:
            return text
        best = min(
            range(1, len(words)),
            key=lambda index: abs(
                len(" ".join(words[:index])) - len(" ".join(words[index:]))
            ),
        )
        return " ".join(words[:best]) + "\n" + " ".join(words[best:])

    def render_ass(self, events: list[CaptionEvent]) -> str:
        c = self.caption_configuration
        header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {self.configuration.width}
PlayResY: {self.configuration.height}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Preview,{c.font_name},{c.font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,5,2,2,80,80,260,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        lines = []
        for event in events:
            text = escape_ass_text(event.text).replace("\n", r"\N")
            lines.append(
                f"Dialogue: 0,{ass_time(event.start)},{ass_time(event.end)},"
                f"Preview,,0,0,0,,{text}"
            )
        return header + "\n".join(lines) + ("\n" if lines else "")

    def build_ffmpeg_command(
        self, source: Path, output: Path, candidate: dict[str, Any],
        source_has_audio: bool, subtitle_path: Path | None,
    ) -> list[str]:
        c = self.configuration
        filters = (
            f"[0:v]setpts=PTS-STARTPTS,split=2[bgsrc][fgsrc];"
            f"[bgsrc]scale={c.width}:{c.height}:force_original_aspect_ratio=increase,"
            f"crop={c.width}:{c.height},setsar=1,boxblur=30:10,"
            f"eq=brightness=-0.16[bg];"
            f"[fgsrc]scale={c.width}:{c.height}:force_original_aspect_ratio=decrease,"
            f"setsar=1[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2"
        )
        if subtitle_path is not None:
            escaped = str(subtitle_path).replace("\\", r"\\").replace(":", r"\:").replace("'", r"\'")
            filters += f",ass=filename='{escaped}'"
        filters += ",setsar=1[vout]"
        if source_has_audio:
            filters += ";[0:a:0]asetpts=PTS-STARTPTS[aout]"
        command = [
            self.ffmpeg_path, "-hide_banner", "-y", "-ss", f"{candidate['start']:.3f}",
            "-t", f"{candidate['duration']:.3f}", "-i", str(source),
            "-filter_complex", filters, "-map", "[vout]",
        ]
        if source_has_audio:
            command += ["-map", "[aout]", "-c:a", c.audio_codec, "-b:a", c.audio_bitrate]
        else:
            command += ["-an"]
        command += [
            "-c:v", c.video_codec, "-pix_fmt", c.pixel_format,
            "-r", f"{c.frame_rate:g}", "-crf", str(c.crf), "-preset", c.preset,
            "-movflags", "+faststart", "-avoid_negative_ts", "make_zero", str(output),
        ]
        return command

    @staticmethod
    def display_command(command: Sequence[str]) -> str:
        return shlex.join(command)

    def _validate_output_probe(
        self, probe: MediaProbe, requested_duration: float, source_has_audio: bool
    ) -> None:
        c = self.configuration
        if probe.width != c.width or probe.height != c.height:
            raise PreviewError(
                f"Rendered dimensions are {probe.width}x{probe.height}; "
                f"expected {c.width}x{c.height}."
            )
        if abs(probe.duration - requested_duration) > self.duration_tolerance:
            raise PreviewError(
                f"Rendered duration {probe.duration:.3f}s is not close to "
                f"requested duration {requested_duration:.3f}s."
            )
        if source_has_audio and not probe.has_audio:
            raise PreviewError("Rendered preview is missing source audio.")

    def _publish(
        self, temporary_video: Path, final_video: Path, metadata_path: Path,
        context: dict[str, Any], probe: MediaProbe,
    ) -> None:
        candidate = context["candidate"]
        configuration = asdict(self.configuration)
        configuration.pop("pixel_format")
        metadata = {
            "version": 1, "video_id": context["record"]["video_id"],
            "candidate_id": candidate["candidate_id"],
            "candidate_rank": candidate["rank"],
            "source_media_path": str(context["media_path"]),
            "source_transcript_path": str(context["transcript_path"]),
            "source_candidates_path": str(context["candidates_path"]),
            "start": round(candidate["start"], 3), "end": round(candidate["end"], 3),
            "duration": round(candidate["duration"], 3),
            "output_path": str(final_video.resolve()), "created_at": utc_now(),
            "render_configuration": configuration,
            "caption_configuration": {
                "font_name": self.caption_configuration.font_name,
                "font_size": self.caption_configuration.font_size,
                "maximum_words": self.caption_configuration.maximum_words,
                "maximum_characters": self.caption_configuration.maximum_characters,
                "maximum_duration_seconds": self.caption_configuration.maximum_duration_seconds,
            },
            "probe": {
                "duration": round(probe.duration, 3), "width": probe.width,
                "height": probe.height, "video_codec": probe.video_codec,
                "audio_codec": probe.audio_codec,
            },
        }
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_temp: Path | None = None
        backup: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=metadata_path.parent,
                prefix=".preview.json.", suffix=".tmp", delete=False,
            ) as stream:
                metadata_temp = Path(stream.name)
                json.dump(metadata, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            if final_video.exists():
                backup = final_video.with_name(f".{final_video.name}.backup.tmp")
                os.replace(final_video, backup)
            os.replace(temporary_video, final_video)
            try:
                os.replace(metadata_temp, metadata_path)
                metadata_temp = None
            except OSError:
                final_video.unlink(missing_ok=True)
                if backup is not None:
                    os.replace(backup, final_video)
                    backup = None
                raise
            if backup is not None:
                backup.unlink(missing_ok=True)
        finally:
            if metadata_temp is not None:
                metadata_temp.unlink(missing_ok=True)
            if backup is not None and backup.exists() and not final_video.exists():
                os.replace(backup, final_video)

    def _write_temporary_ass(self, events: list[CaptionEvent], directory: Path) -> Path:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=directory,
            prefix=".captions.", suffix=".tmp.ass", delete=False,
        ) as stream:
            stream.write(self.render_ass(events))
            return Path(stream.name)

    def _run(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        try:
            return self.command_runner(
                list(command), capture_output=True, text=True, check=False
            )
        except FileNotFoundError as error:
            raise PreviewError(f"Executable is unavailable: {command[0]}") from error

    def _require_executable(self, executable: str, label: str) -> None:
        if os.path.sep in executable:
            available = Path(executable).is_file() and os.access(executable, os.X_OK)
        else:
            available = self.executable_finder(executable) is not None
        if not available:
            raise PreviewError(
                f"{label} executable {executable!r} is unavailable. "
                f"Install it separately or pass an explicit executable path."
            )

    @staticmethod
    def _read_json(path: Path, label: str) -> dict[str, Any]:
        if not path.is_file():
            raise PreviewError(f"The {label} does not exist: {path}")
        try:
            with path.open(encoding="utf-8") as stream:
                document = json.load(stream)
        except json.JSONDecodeError as error:
            raise PreviewError(f"The {label} contains invalid JSON: {error}") from error
        if not isinstance(document, dict):
            raise PreviewError(f"The {label} must contain a JSON object.")
        return document

    def _output_paths(
        self, video_id: str, candidate_id: str, output_path: Path
    ) -> tuple[Path, Path]:
        final = Path(output_path).expanduser().resolve()
        if final.suffix.lower() == ".mp4":
            return final, final.with_name("preview.json")
        return final / video_id / candidate_id / "preview.mp4", final / video_id / candidate_id / "preview.json"

    def _valid_existing(self, video: Path, metadata: Path) -> bool:
        if not video.is_file() or not metadata.is_file():
            return False
        try:
            document = self._read_json(metadata, "preview metadata")
            return (
                document.get("version") == 1
                and Path(document.get("output_path", "")).resolve() == video.resolve()
            )
        except (PreviewError, OSError):
            return False

    def _result(
        self, status: PreviewResultStatus, message: str, context: dict[str, Any],
        video: Path, metadata: Path, *, command: Sequence[str] = (), rendered: bool = False,
    ) -> PreviewResult:
        candidate = context["candidate"]
        return PreviewResult(
            status, message, context["record"]["video_id"],
            candidate["candidate_id"], candidate["rank"], candidate["start"],
            candidate["end"], candidate["duration"], str(video), str(metadata),
            tuple(command), rendered,
        )
