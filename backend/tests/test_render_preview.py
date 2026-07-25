from backend.app import render_preview
from backend.services.video_preview_renderer import PreviewResult, PreviewResultStatus


class FakeRenderer:
    instances = []

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.calls = []
        self.instances.append(self)

    def render(self, video_id, **kwargs):
        self.calls.append((video_id, kwargs))
        return PreviewResult(
            PreviewResultStatus.SUCCESS, "Preview rendered and verified.", video_id,
            "candidate", 1, 1.0, 3.0, 2.0, "/tmp/preview.mp4",
            "/tmp/preview.json", ("ffmpeg", "-i", "source", "preview.mp4"), True,
        )

    @staticmethod
    def display_command(command):
        return " ".join(command)


def test_cli_defaults_and_printed_summary(monkeypatch, capsys):
    FakeRenderer.instances.clear()
    monkeypatch.setattr(render_preview, "VideoPreviewRenderer", FakeRenderer)
    assert render_preview.main(["--video-id", "video"]) == 0
    instance = FakeRenderer.instances[-1]
    assert instance.calls[0][1]["rank"] is None
    assert instance.kwargs["configuration"].width == 1080
    assert instance.kwargs["configuration"].captions_enabled
    output = capsys.readouterr().out
    assert "Selected candidate: candidate (rank 1)" in output
    assert "Preview path: /tmp/preview.mp4" in output
    assert "Summary: success" in output


def test_cli_candidate_id_no_captions_force_and_dry_run(monkeypatch, capsys):
    FakeRenderer.instances.clear()
    monkeypatch.setattr(render_preview, "VideoPreviewRenderer", FakeRenderer)
    assert render_preview.main([
        "--video-id", "video", "--candidate-id", "candidate",
        "--no-captions", "--force", "--dry-run",
    ]) == 0
    instance = FakeRenderer.instances[-1]
    assert instance.calls[0][1]["candidate_id"] == "candidate"
    assert instance.calls[0][1]["force"]
    assert instance.calls[0][1]["dry_run"]
    assert not instance.kwargs["configuration"].captions_enabled
    assert "command was not rendered" in capsys.readouterr().out


def test_cli_rank(monkeypatch):
    FakeRenderer.instances.clear()
    monkeypatch.setattr(render_preview, "VideoPreviewRenderer", FakeRenderer)
    assert render_preview.main(["--video-id", "video", "--rank", "2"]) == 0
    assert FakeRenderer.instances[-1].calls[0][1]["rank"] == 2


def test_cli_failed_result_returns_nonzero(monkeypatch):
    class FailedRenderer(FakeRenderer):
        def render(self, video_id, **kwargs):
            return PreviewResult(PreviewResultStatus.FAILED, "failed", video_id)

    monkeypatch.setattr(render_preview, "VideoPreviewRenderer", FailedRenderer)
    assert render_preview.main(["--video-id", "video"]) == 1


def test_cli_validation_errors():
    invalid = [
        ["--video-id", "v", "--rank", "0"],
        ["--video-id", "v", "--rank", "1", "--candidate-id", "x"],
        ["--video-id", "v", "--width", "101"],
        ["--video-id", "v", "--height", "-2"],
        ["--video-id", "v", "--frame-rate", "0"],
        ["--video-id", "v", "--crf", "52"],
        ["--video-id", "v", "--preset", "unsafe"],
        ["--video-id", "v", "--caption-font-size", "0"],
    ]
    for argv in invalid:
        try:
            render_preview.main(argv)
        except SystemExit as error:
            assert error.code == 2
        else:
            raise AssertionError(f"Expected argparse failure for {argv}")
