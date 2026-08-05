import json
import os
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from backend.services.video_manifest import (
    DEFAULT_MANIFEST_PATH,
    TranscriptionStatus,
    VideoManifest,
    VideoStatus,
    utc_now,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TRANSCRIPT_DIRECTORY = PROJECT_ROOT / "data" / "transcripts"


class TranscriptionResultStatus(str, Enum):
    SUCCESS = "success"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class TranscriptionResult:
    video_id: str
    status: TranscriptionResultStatus
    message: str
    transcript_json_path: str | None = None
    transcript_text_path: str | None = None
    subtitle_srt_path: str | None = None


@dataclass(frozen=True)
class TranscriptionBatchResult:
    results: list[TranscriptionResult]

    @property
    def successful(self) -> int:
        return sum(
            result.status is TranscriptionResultStatus.SUCCESS
            for result in self.results
        )

    @property
    def skipped(self) -> int:
        return sum(
            result.status is TranscriptionResultStatus.SKIPPED
            for result in self.results
        )

    @property
    def failed(self) -> int:
        return sum(
            result.status is TranscriptionResultStatus.FAILED
            for result in self.results
        )


class VideoTranscriber:
    """Transcribe downloaded local media into timestamped artifacts."""

    def __init__(
        self,
        manifest_path: Path = DEFAULT_MANIFEST_PATH,
        output_directory: Path = DEFAULT_TRANSCRIPT_DIRECTORY,
        *,
        manifest: VideoManifest | None = None,
        model: Any | None = None,
        model_factory: Callable[..., Any] | None = None,
        model_name: str = "base.en",
        device: str = "cpu",
        compute_type: str = "int8",
        language: str = "en",
    ) -> None:
        self.manifest = manifest or VideoManifest(manifest_path)
        self.output_directory = Path(output_directory)
        self._model = model
        self._model_factory = model_factory
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self.language = language

    def transcribe(
        self,
        *,
        limit: int | None = None,
        video_id: str | None = None,
        retry_failed: bool = False,
        force: bool = False,
    ) -> TranscriptionBatchResult:
        """Transcribe selected records and continue past individual failures."""

        records = self.manifest.read_records()
        if video_id is not None:
            records = [
                record for record in records if record["video_id"] == video_id
            ]

        selected = []
        for record in records:
            if record["status"] != VideoStatus.DOWNLOADED.value:
                continue
            state = record["transcription"]["status"]
            if (
                state == TranscriptionStatus.FAILED.value
                and not retry_failed
            ):
                continue
            selected.append(record)
        if limit is not None:
            selected = selected[:limit]

        results = []
        for record in selected:
            state = record["transcription"]["status"]
            if state == TranscriptionStatus.COMPLETED.value and not force:
                results.append(
                    TranscriptionResult(
                        record["video_id"],
                        TranscriptionResultStatus.SKIPPED,
                        "Transcription is already completed.",
                    )
                )
                continue
            results.append(self._transcribe_record(record))
        return TranscriptionBatchResult(results)

    def _transcribe_record(
        self, record: dict[str, Any]
    ) -> TranscriptionResult:
        video_id = record["video_id"]
        local_path = record["local_file_path"]
        if not isinstance(local_path, str) or not local_path.strip():
            return self._failure(video_id, "Local media path is missing.")
        try:
            media_path = self.manifest.paths.resolve(
                local_path, must_exist=True, regular=True
            )
        except ValueError:
            return self._failure(
                video_id,
                "Local media file does not exist or is outside persistent storage.",
            )

        started_at = utc_now()
        self.manifest.update_transcription(
            video_id,
            status=TranscriptionStatus.PROCESSING.value,
            model=self.model_name,
            language=self.language,
            started_at=started_at,
            completed_at=None,
            transcript_json_path=None,
            transcript_text_path=None,
            subtitle_srt_path=None,
            error_message=None,
        )
        try:
            model = self._get_model()
            segment_generator, info = model.transcribe(
                str(media_path),
                word_timestamps=True,
                vad_filter=True,
                language=self.language,
                beam_size=5,
            )
            segments = [
                self._serialize_segment(segment)
                for segment in segment_generator
            ]
            text = " ".join(
                segment["text"].strip()
                for segment in segments
                if segment["text"].strip()
            )
            paths = self._write_artifacts(
                video_id, media_path, info, segments, text
            )
            self.manifest.update_transcription(
                video_id,
                status=TranscriptionStatus.COMPLETED.value,
                model=self.model_name,
                language=self._optional_attr(info, "language")
                or self.language,
                completed_at=utc_now(),
                transcript_json_path=str(paths["json"]),
                transcript_text_path=str(paths["text"]),
                subtitle_srt_path=str(paths["srt"]),
                error_message=None,
            )
            return TranscriptionResult(
                video_id,
                TranscriptionResultStatus.SUCCESS,
                "Transcription completed.",
                str(paths["json"]),
                str(paths["text"]),
                str(paths["srt"]),
            )
        except Exception as error:
            return self._failure(video_id, f"Transcription failed: {error}")

    def _failure(self, video_id: str, message: str) -> TranscriptionResult:
        self.manifest.update_transcription(
            video_id,
            status=TranscriptionStatus.FAILED.value,
            completed_at=None,
            transcript_json_path=None,
            transcript_text_path=None,
            subtitle_srt_path=None,
            error_message=message,
        )
        return TranscriptionResult(
            video_id, TranscriptionResultStatus.FAILED, message
        )

    def _get_model(self) -> Any:
        if self._model is not None:
            return self._model
        if self._model_factory is not None:
            factory = self._model_factory
        else:
            try:
                from faster_whisper import WhisperModel
            except ImportError as error:
                raise RuntimeError(
                    "faster-whisper is not installed. Install transcription "
                    "dependencies with: python -m pip install -r "
                    "backend/requirements-transcription.txt"
                ) from error
            factory = WhisperModel
        self._model = factory(
            self.model_name,
            device=self.device,
            compute_type=self.compute_type,
        )
        return self._model

    @classmethod
    def _serialize_segment(cls, segment: Any) -> dict[str, Any]:
        words = getattr(segment, "words", None) or []
        return {
            "id": getattr(segment, "id", None),
            "start": cls._number_or_none(getattr(segment, "start", None)),
            "end": cls._number_or_none(getattr(segment, "end", None)),
            "text": str(getattr(segment, "text", "") or ""),
            "words": [
                {
                    "start": cls._number_or_none(
                        getattr(word, "start", None)
                    ),
                    "end": cls._number_or_none(getattr(word, "end", None)),
                    "word": str(getattr(word, "word", "") or ""),
                    "probability": cls._number_or_none(
                        getattr(word, "probability", None)
                    ),
                }
                for word in words
            ],
        }

    @staticmethod
    def _number_or_none(value: Any) -> int | float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return value

    @staticmethod
    def _optional_attr(value: Any, name: str) -> Any:
        return getattr(value, name, None)

    def _write_artifacts(
        self,
        video_id: str,
        media_path: Path,
        info: Any,
        segments: list[dict[str, Any]],
        text: str,
    ) -> dict[str, Path]:
        directory = self.output_directory / video_id
        directory.mkdir(parents=True, exist_ok=True)
        paths = {
            "json": directory / "transcript.json",
            "text": directory / "transcript.txt",
            "srt": directory / "subtitles.srt",
        }
        document = {
            "version": 1,
            "video_id": video_id,
            "source_media_path": self.manifest.paths.store(media_path),
            "model": self.model_name,
            "language": self._optional_attr(info, "language")
            or self.language,
            "language_probability": self._number_or_none(
                self._optional_attr(info, "language_probability")
            ),
            "duration_seconds": self._number_or_none(
                self._optional_attr(info, "duration")
            ),
            "created_at": utc_now(),
            "text": text,
            "segments": segments,
        }
        contents = {
            "json": json.dumps(document, indent=2) + "\n",
            "text": text + ("\n" if text else ""),
            "srt": self._render_srt(segments),
        }
        temporary_paths: list[Path] = []
        try:
            for key, destination in paths.items():
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=directory,
                    prefix=f".{destination.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as temporary:
                    temporary.write(contents[key])
                    temporary.flush()
                    os.fsync(temporary.fileno())
                    temporary_paths.append(Path(temporary.name))
            for temporary, destination in zip(
                temporary_paths, paths.values(), strict=True
            ):
                os.replace(temporary, destination)
            return paths
        finally:
            for temporary in temporary_paths:
                temporary.unlink(missing_ok=True)

    @classmethod
    def _render_srt(cls, segments: list[dict[str, Any]]) -> str:
        blocks = []
        for segment in segments:
            text = segment["text"].strip()
            start = segment["start"]
            end = segment["end"]
            if not text or start is None or end is None:
                continue
            blocks.append(
                f"{len(blocks) + 1}\n"
                f"{cls._srt_time(start)} --> {cls._srt_time(end)}\n{text}"
            )
        return "\n\n".join(blocks) + ("\n" if blocks else "")

    @staticmethod
    def _srt_time(seconds: int | float) -> str:
        milliseconds = max(0, round(seconds * 1000))
        hours, remainder = divmod(milliseconds, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        whole_seconds, milliseconds = divmod(remainder, 1000)
        return (
            f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},"
            f"{milliseconds:03d}"
        )
