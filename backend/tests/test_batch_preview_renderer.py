import json
from pathlib import Path

import pytest

from backend.services.batch_preview_renderer import BatchPreviewRenderer
from backend.services.video_preview_renderer import PreviewResult, PreviewResultStatus, VideoPreviewRenderer


def candidates():
    return [
        {"candidate_id": f"c{rank}", "rank": rank, "score": 80-rank,
         "start": rank * 10.0, "end": rank * 10.0 + 5, "duration": 5.0,
         "text": f"candidate {rank}"}
        for rank in range(1, 5)
    ]


class FakeManifest:
    def __init__(self, artifact):
        self.artifact = artifact
    def get(self, video_id):
        return {"clip_analysis": {"status": "completed", "candidates_json_path": str(self.artifact)}}


class FakeRenderer:
    _read_json = staticmethod(VideoPreviewRenderer._read_json)
    _validate_candidate = staticmethod(VideoPreviewRenderer._validate_candidate)
    def __init__(self, artifact, root, failures=()):
        self.manifest = FakeManifest(artifact)
        self.root = root
        self.failures = set(failures)
        self.calls = []
    def render(self, video_id, **kwargs):
        rank = kwargs["rank"]
        self.calls.append(kwargs)
        if rank in self.failures:
            return PreviewResult(PreviewResultStatus.FAILED, "boom", video_id)
        directory = self.root / f"c{rank}"
        directory.mkdir(parents=True)
        preview = directory / "preview.mp4"
        metadata = directory / "preview.json"
        preview.write_bytes(b"video")
        metadata.write_text(json.dumps({
            "version": 1, "video_id": video_id, "candidate_id": f"c{rank}",
            "output_path": str(preview.resolve()),
        }))
        status = PreviewResultStatus.SKIPPED if rank == 2 else PreviewResultStatus.SUCCESS
        return PreviewResult(status, "done", video_id, f"c{rank}", rank,
                             output_path=str(preview), metadata_path=str(metadata))


class FakeQueue:
    def __init__(self, existing=None):
        self.calls = []
        self.existing = existing
    def find_by_candidate(self, video_id, candidate_id):
        return self.existing
    def add_or_update_preview(self, video_id, candidate, preview, metadata):
        self.calls.append((video_id, candidate, preview, metadata))
        return {"review_id": f"review-{candidate['rank']}"}


def setup(tmp_path, count=4, failures=()):
    artifact = tmp_path / "candidates.json"
    artifact.write_text(json.dumps({"version": 1, "video_id": "video",
                                    "candidates": candidates()[:count]}))
    renderer = FakeRenderer(artifact, tmp_path / "out", failures)
    queue = FakeQueue()
    return BatchPreviewRenderer(renderer, queue), renderer, queue


def test_default_top_three_and_queue_registration(tmp_path):
    batch, renderer, queue = setup(tmp_path)
    result = batch.render("video")
    assert [item.rank for item in result.items] == [1, 2, 3]
    assert result.successful == 2 and result.skipped == 1 and result.failed == 0
    assert [call[1]["rank"] for call in queue.calls] == [1, 2, 3]
    assert result.items[1].review_id == "review-2"


def test_fewer_candidates_and_force_forwarding(tmp_path):
    batch, renderer, _ = setup(tmp_path, count=2)
    result = batch.render("video", top=3, force=True)
    assert [item.rank for item in result.items] == [1, 2]
    assert all(call["force"] for call in renderer.calls)


def test_explicit_order_missing_and_continues_after_failure(tmp_path):
    batch, renderer, queue = setup(tmp_path, failures=(1,))
    result = batch.render("video", ranks=[3, 9, 1, 2])
    assert [item.rank for item in result.items] == [3, 9, 1, 2]
    assert result.failed == 2 and result.skipped == 1 and result.successful == 1
    assert [call[1]["rank"] for call in queue.calls] == [3, 2]


@pytest.mark.parametrize("ranks", [[1, 1], [0], [-1]])
def test_invalid_explicit_ranks(tmp_path, ranks):
    batch, _, _ = setup(tmp_path)
    with pytest.raises(ValueError):
        batch.render("video", ranks=ranks)


def test_dry_run_does_not_touch_queue(tmp_path):
    batch, _, queue = setup(tmp_path)
    result = batch.render("video", dry_run=True)
    assert not queue.calls
    assert result.failed == 0


def test_verification_failure_not_added(tmp_path):
    batch, renderer, queue = setup(tmp_path)
    original = renderer.render
    def bad(*args, **kwargs):
        result = original(*args, **kwargs)
        Path(result.output_path).unlink()
        return result
    renderer.render = bad
    result = batch.render("video", ranks=[1])
    assert result.failed == 1 and not queue.calls


def test_existing_adjustment_is_used_for_forced_rerender(tmp_path):
    batch, renderer, _ = setup(tmp_path, count=1)
    queue = FakeQueue({
        "render_start": 7.0, "render_end": 15.0, "timing_revision": 3,
    })
    batch.review_queue = queue
    batch.render("video", force=True)
    assert renderer.calls[0]["render_start"] == 7.0
    assert renderer.calls[0]["render_end"] == 15.0
    assert renderer.calls[0]["timing_revision"] == 3


@pytest.mark.parametrize("mutation", [
    lambda values: values[1].update(rank=1),
    lambda values: values[1].update(candidate_id="c1"),
    lambda values: values[0].update(score=101),
])
def test_invalid_artifact_rejected_before_render(tmp_path, mutation):
    values = candidates()
    mutation(values)
    artifact = tmp_path / "candidates.json"
    artifact.write_text(json.dumps({
        "version": 1, "video_id": "video", "candidates": values,
    }))
    renderer = FakeRenderer(artifact, tmp_path / "out")
    with pytest.raises(ValueError):
        BatchPreviewRenderer(renderer, FakeQueue()).render("video")
    assert not renderer.calls
