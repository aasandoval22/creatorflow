import json
import subprocess
from pathlib import Path

import pytest

from backend.services.video_preview_renderer import (
    CaptionConfiguration,
    MediaProbe,
    PreviewError,
    PreviewResultStatus,
    RenderConfiguration,
    VideoPreviewRenderer,
    ass_time,
    escape_ass_text,
)


def write_json(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


@pytest.fixture
def preview_files(tmp_path):
    media = tmp_path / "source.mp4"
    media.write_bytes(b"media")
    transcript = write_json(tmp_path / "transcript.json", {
        "version": 1, "video_id": "video", "segments": [{
            "id": 1, "start": 0, "end": 10, "text": "Outside hello, world. tail",
            "words": [
                {"start": 0.0, "end": 0.3, "word": "Outside"},
                {"start": 1.0, "end": 1.4, "word": " hello,"},
                {"start": 1.5, "end": 1.9, "word": " world."},
                {"start": 9.0, "end": 9.4, "word": " tail"},
            ],
        }],
    })
    candidates = write_json(tmp_path / "candidates.json", {
        "version": 1, "video_id": "video", "candidates": [
            {"rank": 1, "candidate_id": "one", "start": 1.0, "end": 3.0,
             "duration": 2.0, "text": "hello world"},
            {"rank": 2, "candidate_id": "two", "start": 3.0, "end": 5.0,
             "duration": 2.0, "text": "other"},
        ],
    })
    manifest = write_json(tmp_path / "manifest.json", {
        "version": 3, "videos": [{
            "video_id": "video", "source_platform": "youtube",
            "channel_name": None, "channel_url": None, "video_url": "url",
            "title": None, "uploader": None, "upload_date": None,
            "duration_seconds": 10, "discovered_at": "2020-01-01T00:00:00+00:00",
            "downloaded_at": "2020-01-01T00:00:00+00:00",
            "local_file_path": str(media), "status": "downloaded",
            "error_message": None,
            "transcription": {
                "status": "completed", "model": "test", "language": "en",
                "started_at": "2020-01-01T00:00:00+00:00",
                "completed_at": "2020-01-01T00:00:00+00:00",
                "transcript_json_path": str(transcript),
                "transcript_text_path": None, "subtitle_srt_path": None,
                "error_message": None,
            },
            "clip_analysis": {
                "status": "completed",
                "started_at": "2020-01-01T00:00:00+00:00",
                "completed_at": "2020-01-01T00:00:00+00:00",
                "candidate_count": 2, "candidates_json_path": str(candidates),
                "error_message": None,
            },
        }],
    })
    return manifest, media, transcript, candidates


class FakeRunner:
    def __init__(self, *, source_audio=True, output_duration=2.0, fail_ffmpeg=False):
        self.commands = []
        self.source_audio = source_audio
        self.output_duration = output_duration
        self.fail_ffmpeg = fail_ffmpeg

    def __call__(self, command, **kwargs):
        self.commands.append((command, kwargs))
        if "ffprobe" in Path(command[0]).name:
            is_output = ".tmp.mp4" in command[-1]
            streams = [{
                "codec_type": "video", "codec_name": "h264",
                "width": 1080 if is_output else 1920,
                "height": 1920 if is_output else 1080,
            }]
            if self.source_audio:
                streams.append({"codec_type": "audio", "codec_name": "aac"})
            return subprocess.CompletedProcess(
                command, 0, json.dumps({
                    "format": {"duration": str(self.output_duration if is_output else 10)},
                    "streams": streams,
                }), "",
            )
        if self.fail_ffmpeg:
            return subprocess.CompletedProcess(command, 1, "", "encoder exploded")
        Path(command[-1]).write_bytes(b"preview")
        return subprocess.CompletedProcess(command, 0, "", "")


def renderer(preview_files, runner=None, **kwargs):
    manifest, _, _, _ = preview_files
    return VideoPreviewRenderer(
        manifest, manifest.parent / "previews",
        command_runner=runner or FakeRunner(),
        executable_finder=lambda value: f"/usr/bin/{value}",
        **kwargs,
    )


def test_selection_by_default_rank_rank_and_id(preview_files):
    service = renderer(preview_files)
    assert service.prepare("video")["candidate"]["candidate_id"] == "one"
    assert service.prepare("video", rank=2)["candidate"]["candidate_id"] == "two"
    assert service.prepare("video", candidate_id="two")["candidate"]["rank"] == 2


def test_selection_is_mutually_exclusive_and_missing(preview_files):
    service = renderer(preview_files)
    with pytest.raises(PreviewError, match="mutually exclusive"):
        service.prepare("video", rank=1, candidate_id="one")
    with pytest.raises(PreviewError, match="No candidate"):
        service.prepare("video", rank=99)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d["videos"][0].update(status="discovered"), "downloaded status"),
        (lambda d: d["videos"][0].update(local_file_path=None), "media path"),
        (lambda d: d["videos"][0]["clip_analysis"].update(status="failed"), "not completed"),
        (lambda d: d["videos"][0]["transcription"].update(status="failed"), "not completed"),
    ],
)
def test_manifest_state_validation(preview_files, change, message):
    manifest = preview_files[0]
    document = json.loads(manifest.read_text())
    change(document)
    manifest.write_text(json.dumps(document))
    result = renderer(preview_files).render("video")
    assert result.status is PreviewResultStatus.FAILED
    assert message in result.message


def test_missing_video_and_media(preview_files):
    assert renderer(preview_files).render("missing").status is PreviewResultStatus.FAILED
    preview_files[1].unlink()
    assert "does not exist" in renderer(preview_files).render("video").message


@pytest.mark.parametrize(
    "candidate",
    [
        {"rank": 1, "candidate_id": "x", "start": -1, "end": 2, "duration": 3, "text": "x"},
        {"rank": 1, "candidate_id": "x", "start": 2, "end": 2, "duration": 0, "text": "x"},
        {"rank": 1, "candidate_id": "x", "start": 1, "end": 2, "duration": 9, "text": "x"},
    ],
)
def test_invalid_candidate_ranges(preview_files, candidate):
    path = preview_files[3]
    document = json.loads(path.read_text())
    document["candidates"] = [candidate]
    path.write_text(json.dumps(document))
    assert renderer(preview_files).render("video").status is PreviewResultStatus.FAILED


def test_candidate_outside_source(preview_files):
    path = preview_files[3]
    document = json.loads(path.read_text())
    document["candidates"][0].update(start=9, end=11, duration=2)
    path.write_text(json.dumps(document))
    assert "exceeds source duration" in renderer(preview_files).render("video").message


def test_probe_malformed_missing_video_and_invalid_duration(preview_files):
    service = renderer(preview_files)
    for stdout, message in [
        ("no", "malformed JSON"),
        (json.dumps({"format": {"duration": "2"}, "streams": []}), "no video"),
        (json.dumps({"format": {"duration": "x"}, "streams": [
            {"codec_type": "video", "codec_name": "h264", "width": 1, "height": 1}
        ]}), "invalid media duration"),
    ]:
        service.command_runner = lambda *a, stdout=stdout, **k: subprocess.CompletedProcess(
            a[0], 0, stdout, ""
        )
        with pytest.raises(PreviewError, match=message):
            service.probe_media(Path("x"))


def test_caption_words_rebase_clip_and_punctuation(preview_files):
    service = renderer(preview_files)
    transcript = json.loads(preview_files[2].read_text())
    events = service.generate_caption_events(transcript, 1, 3)
    assert events == [type(events[0])(0.0, 0.9, "hello, world.")]
    assert "Outside" not in events[0].text
    assert "tail" not in events[0].text


def test_caption_segment_fallback_pause_and_limits(preview_files):
    service = renderer(
        preview_files,
        caption_configuration=CaptionConfiguration(
            maximum_words=2, maximum_characters=10, maximum_duration_seconds=1
        ),
    )
    transcript = {"segments": [
        {"start": 1, "end": 1.6, "text": "first phrase"},
        {"start": 2.5, "end": 3, "text": "second phrase"},
    ]}
    events = service.generate_caption_events(transcript, 1, 3)
    assert len(events) == 2
    assert all(0 <= event.start < event.end <= 2 for event in events)
    assert events[0].end <= events[1].start


def test_ass_time_escaping_and_render(preview_files):
    assert ass_time(61.239) == "0:01:01.24"
    assert escape_ass_text(r"{x}\y") == r"\{x\}\\y"
    service = renderer(preview_files)
    ass = service.render_ass([
        service.generate_caption_events(json.loads(preview_files[2].read_text()), 1, 3)[0]
    ])
    assert "DejaVu Sans,62" in ass
    assert "Dialogue:" in ass


@pytest.mark.parametrize("audio", [True, False])
@pytest.mark.parametrize("captions", [True, False])
def test_ffmpeg_command_is_safe_and_complete(preview_files, audio, captions):
    service = renderer(
        preview_files,
        configuration=RenderConfiguration(captions_enabled=captions),
    )
    command = service.build_ffmpeg_command(
        preview_files[1], Path(".preview.tmp.mp4"),
        {"start": 1.0, "duration": 2.0}, audio,
        Path("captions.ass") if captions else None,
    )
    joined = " ".join(command)
    assert command[0] == "ffmpeg"
    assert "-ss 1.000" in joined and "-t 2.000" in joined
    assert "split=2" in joined and "boxblur" in joined and "overlay=" in joined
    assert "scale=1080:1920" in joined
    assert "libx264" in command and "yuv420p" in command
    assert "+faststart" in command and command[-1].endswith(".tmp.mp4")
    assert ("ass=filename" in joined) is captions
    assert ("-c:a" in command) is audio
    assert ("-an" in command) is (not audio)


def test_success_atomic_artifacts_and_skip(preview_files):
    runner = FakeRunner()
    service = renderer(preview_files, runner)
    result = service.render("video")
    assert result.status is PreviewResultStatus.SUCCESS
    assert Path(result.output_path).read_bytes() == b"preview"
    metadata = json.loads(Path(result.metadata_path).read_text())
    assert metadata["candidate_id"] == "one"
    assert metadata["probe"]["width"] == 1080
    for field in (
        "source_media_path", "source_transcript_path",
        "source_candidates_path", "output_path",
    ):
        assert not Path(metadata[field]).is_absolute()
    assert not list(Path(result.output_path).parent.glob(".*.tmp*"))
    assert service.render("video").status is PreviewResultStatus.SKIPPED


def test_force_failure_preserves_existing_preview(preview_files):
    service = renderer(preview_files, FakeRunner())
    successful = service.render("video")
    output = Path(successful.output_path)
    service.command_runner = FakeRunner(fail_ffmpeg=True)
    failed = service.render("video", force=True)
    assert failed.status is PreviewResultStatus.FAILED
    assert output.read_bytes() == b"preview"
    assert not list(output.parent.glob(".captions.*"))


def test_output_probe_failures(preview_files):
    service = renderer(preview_files)
    with pytest.raises(PreviewError, match="dimensions"):
        service._validate_output_probe(MediaProbe(2, 1, 2, "h264", "aac"), 2, True)
    with pytest.raises(PreviewError, match="duration"):
        service._validate_output_probe(MediaProbe(8, 1080, 1920, "h264", "aac"), 2, True)
    with pytest.raises(PreviewError, match="missing source audio"):
        service._validate_output_probe(MediaProbe(2, 1080, 1920, "h264", None), 2, True)


def test_dry_run_does_not_render_or_create_artifacts(preview_files):
    runner = FakeRunner()
    result = renderer(preview_files, runner).render("video", dry_run=True)
    assert result.status is PreviewResultStatus.SUCCESS
    assert not result.rendered
    assert len(runner.commands) == 1
    assert not Path(result.output_path).exists()


def test_missing_executables(preview_files):
    service = renderer(preview_files)
    service.executable_finder = lambda value: None
    result = service.render("video")
    assert result.status is PreviewResultStatus.FAILED
    assert "FFprobe" in result.message


def test_adjusted_render_uses_window_captions_and_v2_metadata(preview_files):
    runner = FakeRunner(output_duration=4)
    service = renderer(preview_files, runner)
    result = service.render(
        "video", render_start=0, render_end=4, timing_revision=1, force=True
    )
    assert result.status is PreviewResultStatus.SUCCESS
    ffmpeg = next(command for command, _ in runner.commands if command[0] == "ffmpeg")
    assert ffmpeg[ffmpeg.index("-ss") + 1] == "0.000"
    assert ffmpeg[ffmpeg.index("-t") + 1] == "4.000"
    metadata = json.loads(Path(result.metadata_path).read_text())
    assert metadata["version"] == 3
    assert metadata["candidate_start"] == 1
    assert metadata["render_start"] == 0
    assert metadata["render_end"] == 4
    assert metadata["lead_in_seconds"] == 1
    assert metadata["tail_seconds"] == 1
    assert metadata["timing_revision"] == 1
    events = service.generate_caption_events(
        json.loads(preview_files[2].read_text()), 0, 10
    )
    assert "Outside" in " ".join(event.text for event in events)
    assert "tail" in " ".join(event.text for event in events)


def test_adjusted_range_validation_and_legacy_metadata(preview_files):
    service = renderer(preview_files)
    assert service.render("video", render_start=2, render_end=4).status is PreviewResultStatus.FAILED
    assert service.render("video", render_start=-1, render_end=3).status is PreviewResultStatus.FAILED
    output = service.output_directory / "video" / "one" / "preview.mp4"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"old")
    metadata = output.with_name("preview.json")
    metadata.write_text(json.dumps({"version": 1, "output_path": str(output.resolve())}))
    assert service._valid_existing(output, metadata)
