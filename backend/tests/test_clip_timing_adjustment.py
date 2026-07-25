import json
from pathlib import Path

import pytest

from backend.services.clip_review_queue import ClipReviewQueue, ReviewQueueError
from backend.services.clip_timing_adjustment import ClipTimingAdjustmentService
from backend.services.video_preview_renderer import PreviewResult, PreviewResultStatus


class FakeRenderer:
    def __init__(self, *, duration=100, fail=False):
        self.duration = duration
        self.fail = fail
        self.calls = []

    def prepare(self, video_id, *, candidate_id, render_start, render_end):
        if render_start < 0 or render_end > self.duration:
            raise ReviewQueueError("outside source duration")
        if render_start > 10 or render_end < 20 or render_end <= render_start:
            raise ReviewQueueError("must contain candidate")
        return {"render": {
            "start": float(render_start), "end": float(render_end),
            "duration": float(render_end) - float(render_start),
        }}

    def render(self, video_id, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            return PreviewResult(PreviewResultStatus.FAILED, "render failed", video_id)
        output = Path(kwargs["output_path"])
        metadata = output.with_name("preview.json")
        command = ("ffmpeg", "-ss", str(kwargs["render_start"]), "-t",
                   str(kwargs["render_end"] - kwargs["render_start"]))
        if not kwargs["dry_run"]:
            output.write_bytes(b"new preview")
            metadata.write_text(json.dumps({"version": 2}))
        return PreviewResult(
            PreviewResultStatus.SUCCESS, "ok", video_id, "c1", 1,
            kwargs["render_start"], kwargs["render_end"],
            kwargs["render_end"] - kwargs["render_start"],
            str(output), str(metadata), command, not kwargs["dry_run"],
        )


def setup(tmp_path):
    preview = tmp_path / "preview.mp4"
    metadata = tmp_path / "preview.json"
    preview.write_bytes(b"old preview")
    metadata.write_text('{"version": 1}')
    queue = ClipReviewQueue(tmp_path / "reviews.json")
    item = queue.add_or_update_preview("video", {
        "candidate_id": "c1", "rank": 1, "score": 75,
        "start": 10, "end": 20, "duration": 10, "text": "clip",
    }, preview, metadata)
    return queue, item, preview, metadata


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"lead_in": 2}, (8, 20)),
        ({"tail": 3}, (10, 23)),
        ({"lead_in": 2, "tail": 3}, (8, 23)),
        ({"render_start": 7, "render_end": 25}, (7, 25)),
    ],
)
def test_adjustment_modes(tmp_path, kwargs, expected):
    queue, item, _, _ = setup(tmp_path)
    result = ClipTimingAdjustmentService(queue, FakeRenderer()).adjust(
        item["review_id"], **kwargs
    )
    assert (result.item["render_start"], result.item["render_end"]) == expected
    assert result.item["timing_revision"] == 1
    assert result.item["status"] == "pending"


def test_relative_is_from_candidate_and_notes(tmp_path):
    queue, item, _, _ = setup(tmp_path)
    queue.approve(item["review_id"], "old")
    service = ClipTimingAdjustmentService(queue, FakeRenderer())
    first = service.adjust(item["review_id"], lead_in=4, tail=5)
    second = service.adjust(item["review_id"], lead_in=1, note="new")
    assert first.item["render_start"] == 6
    assert second.item["render_start"] == 9 and second.item["render_end"] == 20
    assert second.item["timing_revision"] == 2
    assert second.item["review_note"] == "new"
    cleared = service.adjust(item["review_id"], tail=1, clear_note=True)
    assert cleared.item["review_note"] is None


@pytest.mark.parametrize("kwargs", [
    {},
    {"lead_in": 1, "render_start": 1, "render_end": 20},
    {"render_start": 1},
    {"tail": -1},
])
def test_adjustment_validation(tmp_path, kwargs):
    queue, item, _, _ = setup(tmp_path)
    with pytest.raises(ReviewQueueError):
        ClipTimingAdjustmentService(queue, FakeRenderer()).adjust(item["review_id"], **kwargs)


def test_maximum_source_override_dry_run_and_reset(tmp_path):
    queue, item, preview, _ = setup(tmp_path)
    service = ClipTimingAdjustmentService(queue, FakeRenderer())
    with pytest.raises(ReviewQueueError, match="maximum"):
        service.adjust(item["review_id"], render_start=0, render_end=80)
    long = service.adjust(
        item["review_id"], render_start=0, render_end=80,
        allow_longer=True, dry_run=True,
    )
    assert long.dry_run and preview.read_bytes() == b"old preview"
    assert queue.find_by_review_id(item["review_id"])["timing_revision"] == 0
    adjusted = service.adjust(item["review_id"], lead_in=2)
    reset = service.reset(item["review_id"])
    assert adjusted.item["render_start"] == 8
    assert reset.item["render_start"] == 10 and reset.item["render_end"] == 20
    assert reset.item["timing_revision"] == 2


def test_render_and_queue_failures_preserve_artifacts_and_item(tmp_path, monkeypatch):
    queue, item, preview, metadata = setup(tmp_path)
    with pytest.raises(ReviewQueueError):
        ClipTimingAdjustmentService(queue, FakeRenderer(fail=True)).adjust(
            item["review_id"], tail=2
        )
    assert preview.read_bytes() == b"old preview"
    before = queue.find_by_review_id(item["review_id"])
    monkeypatch.setattr(queue, "_write_document", lambda document: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        ClipTimingAdjustmentService(queue, FakeRenderer()).adjust(item["review_id"], tail=2)
    assert preview.read_bytes() == b"old preview"
    assert metadata.read_text() == '{"version": 1}'
    assert ClipReviewQueue(queue.path).find_by_review_id(item["review_id"]) == before
