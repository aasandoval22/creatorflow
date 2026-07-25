from backend.app import render_previews
from backend.services.batch_preview_renderer import BatchCandidateResult, BatchPreviewResult


class FakeRenderer:
    instances = []
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.instances.append(self)


class FakeQueue:
    instances = []
    def __init__(self, path):
        self.path = path
        self.instances.append(self)


class FakeBatch:
    instances = []
    result = BatchPreviewResult("video", (
        BatchCandidateResult(1, "c1", "success", "rendered", "/preview.mp4",
                             "/preview.json", "review-1"),
        BatchCandidateResult(2, "c2", "skipped", "exists", "/two.mp4",
                             "/two.json", "review-2"),
    ))
    def __init__(self, renderer, queue):
        self.renderer, self.queue, self.calls = renderer, queue, []
        self.instances.append(self)
    def render(self, video_id, **kwargs):
        self.calls.append((video_id, kwargs))
        return self.result


def install(monkeypatch):
    FakeRenderer.instances.clear()
    FakeQueue.instances.clear()
    FakeBatch.instances.clear()
    monkeypatch.setattr(render_previews, "VideoPreviewRenderer", FakeRenderer)
    monkeypatch.setattr(render_previews, "ClipReviewQueue", FakeQueue)
    monkeypatch.setattr(render_previews, "BatchPreviewRenderer", FakeBatch)


def test_defaults_output_and_explicit_argv(monkeypatch, capsys):
    install(monkeypatch)
    assert render_previews.main(["--video-id", "video"]) == 0
    call = FakeBatch.instances[-1].calls[0]
    assert call[1]["top"] == 3 and call[1]["ranks"] is None
    output = capsys.readouterr().out
    assert "Selected candidate: c1 (rank 1)" in output
    assert "Review ID: review-1" in output
    assert "successful=1, skipped=1, failed=0" in output


def test_paths_ranks_dry_run_and_renderer_configuration(monkeypatch):
    install(monkeypatch)
    assert render_previews.main([
        "--video-id", "video", "--manifest-path", "/m.json",
        "--candidates-path", "/c.json", "--output-directory", "/out",
        "--review-queue-path", "/q.json", "--ranks", "3,1", "--dry-run",
        "--force", "--no-captions", "--width", "720", "--height", "1280",
        "--frame-rate", "24", "--crf", "22", "--preset", "fast",
        "--caption-font", "Font", "--caption-font-size", "40",
        "--caption-max-words", "4", "--caption-max-characters", "20",
        "--caption-max-duration", "2", "--ffmpeg-path", "/ffmpeg",
        "--ffprobe-path", "/ffprobe",
    ]) == 0
    renderer = FakeRenderer.instances[-1]
    assert renderer.kwargs["configuration"].width == 720
    assert not renderer.kwargs["configuration"].captions_enabled
    assert renderer.kwargs["caption_configuration"].maximum_words == 4
    assert renderer.kwargs["ffmpeg_path"] == "/ffmpeg"
    assert not FakeQueue.instances  # Dry runs must not create or migrate queue state.
    kwargs = FakeBatch.instances[-1].calls[0][1]
    assert kwargs["ranks"] == [3, 1] and kwargs["dry_run"] and kwargs["force"]
    assert str(kwargs["candidates_path"]) == "/c.json"


def test_failed_item_exit_code(monkeypatch):
    install(monkeypatch)
    FakeBatch.result = BatchPreviewResult("video", (
        BatchCandidateResult(4, None, "failed", "missing"),
    ))
    try:
        assert render_previews.main(["--video-id", "video", "--ranks", "4"]) == 1
    finally:
        FakeBatch.result = BatchPreviewResult("video", (
            BatchCandidateResult(1, "c1", "success", "rendered", "/preview.mp4",
                                 "/preview.json", "review-1"),
            BatchCandidateResult(2, "c2", "skipped", "exists", "/two.mp4",
                                 "/two.json", "review-2"),
        ))


def test_argument_validation():
    invalid = [
        ["--video-id", "v", "--top", "0"],
        ["--video-id", "v", "--ranks", "1,x"],
        ["--video-id", "v", "--ranks", "1,1"],
        ["--video-id", "v", "--top", "2", "--ranks", "1,2"],
        ["--video-id", "v", "--width", "101"],
    ]
    for argv in invalid:
        try:
            render_previews.main(argv)
        except SystemExit as error:
            assert error.code == 2
        else:
            raise AssertionError(f"Expected argparse failure for {argv}")
