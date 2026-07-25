import json
import threading
from pathlib import Path

import pytest

from backend.services.clip_review_queue import ClipReviewQueue, ReviewQueueError


def candidate(candidate_id="c1", rank=1, score=74.7):
    return {
        "candidate_id": candidate_id, "rank": rank, "score": score,
        "start": 10.0, "end": 20.0, "duration": 10.0, "text": "Useful clip",
    }


def add(queue, value=None):
    return queue.add_or_update_preview(
        "video", value or candidate(), "/tmp/preview.mp4", "/tmp/preview.json"
    )


def test_creates_queue_and_stable_identity(tmp_path):
    path = tmp_path / "reviews.json"
    queue = ClipReviewQueue(path)
    first = add(queue)
    second = add(queue)
    assert path.is_file()
    assert first["review_id"] == second["review_id"]
    assert len(queue.list_items()) == 1
    assert first["status"] == "pending"


@pytest.mark.parametrize("change", [
    lambda d: d.update(version=4),
    lambda d: d["items"][0].update(status="maybe"),
    lambda d: d["items"][0].update(created_at="yesterday"),
    lambda d: d["items"][0].update(candidate_score=101),
    lambda d: d["items"][0].update(candidate_rank=0),
    lambda d: d["items"][0].update(candidate_duration=-1),
])
def test_strict_validation(tmp_path, change):
    path = tmp_path / "reviews.json"
    add(ClipReviewQueue(path))
    document = json.loads(path.read_text())
    change(document)
    path.write_text(json.dumps(document))
    with pytest.raises(ReviewQueueError):
        ClipReviewQueue(path)


def test_corrupt_file_is_actionable_and_not_replaced(tmp_path):
    path = tmp_path / "reviews.json"
    path.write_text("{bad")
    with pytest.raises(ReviewQueueError, match="Repair or remove"):
        ClipReviewQueue(path)
    assert path.read_text() == "{bad"


def test_duplicate_identity_rejected(tmp_path):
    path = tmp_path / "reviews.json"
    add(ClipReviewQueue(path))
    document = json.loads(path.read_text())
    document["items"].append(document["items"][0].copy())
    path.write_text(json.dumps(document))
    with pytest.raises(ReviewQueueError, match="Duplicate"):
        ClipReviewQueue(path)


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_update_preserves_decision_and_note(tmp_path, decision):
    queue = ClipReviewQueue(tmp_path / "reviews.json")
    item = add(queue)
    getattr(queue, decision)(item["review_id"], "keep")
    updated = add(queue, candidate(score=80))
    assert updated["status"] == ("approved" if decision == "approve" else "rejected")
    assert updated["review_note"] == "keep"
    assert updated["candidate_score"] == 80
    assert updated["reviewed_at"]


def test_transitions_notes_filters_and_cleanup(tmp_path):
    path = tmp_path / "reviews.json"
    queue = ClipReviewQueue(path)
    one = add(queue)
    two = queue.add_or_update_preview(
        "other", candidate("c2", 2), "/tmp/two.mp4", "/tmp/two.json"
    )
    approved = queue.approve(one["review_id"], "first")
    assert approved["reviewed_at"] and approved["review_note"] == "first"
    pending = queue.return_to_pending(one["review_id"])
    assert pending["reviewed_at"] is None and pending["review_note"] == "first"
    assert queue.update_note(one["review_id"], "replacement")["review_note"] == "replacement"
    assert queue.update_note(one["review_id"], None)["review_note"] is None
    rejected = queue.reject(two["review_id"])
    assert rejected["status"] == "rejected"
    assert len(queue.list_items(status="pending")) == 1
    assert queue.list_items(video_id="other")[0]["review_id"] == two["review_id"]
    assert not list(tmp_path.glob(".reviews.*"))


def test_atomic_replace_failure_cleans_temporary(tmp_path, monkeypatch):
    queue = ClipReviewQueue(tmp_path / "reviews.json")
    monkeypatch.setattr("backend.services.clip_review_queue.os.replace",
                        lambda *_: (_ for _ in ()).throw(OSError("replace")))
    with pytest.raises(OSError):
        add(queue)
    assert not list(tmp_path.glob(".reviews.*"))


def test_version_one_migrates_atomically_and_preserves_review(tmp_path):
    path = tmp_path / "reviews.json"
    queue = ClipReviewQueue(path)
    reviewed = queue.approve(add(queue)["review_id"], "keep this")
    document = json.loads(path.read_text())
    document["version"] = 1
    timing = {
        "render_start", "render_end", "render_duration", "lead_in_seconds",
        "tail_seconds", "timing_revision", "timing_updated_at",
    }
    for field in timing:
        document["items"][0].pop(field)
    path.write_text(json.dumps(document))

    migrated = ClipReviewQueue(path).find_by_review_id(reviewed["review_id"])
    assert json.loads(path.read_text())["version"] == 3
    assert migrated["status"] == "approved"
    assert migrated["review_note"] == "keep this"
    assert migrated["render_start"] == migrated["candidate_start"]
    assert migrated["render_end"] == migrated["candidate_end"]
    assert migrated["timing_revision"] == 0
    assert not list(tmp_path.glob(".reviews.*"))


@pytest.mark.parametrize("change", [
    lambda item: item.update(render_start=-1),
    lambda item: item.update(render_start=11),
    lambda item: item.update(render_end=19),
    lambda item: item.update(render_duration=99),
    lambda item: item.update(lead_in_seconds=-1),
    lambda item: item.update(tail_seconds=-1),
    lambda item: item.update(timing_revision=-1),
    lambda item: item.update(timing_revision=1.5),
    lambda item: item.update(timing_updated_at="yesterday"),
])
def test_timing_validation(tmp_path, change):
    path = tmp_path / "reviews.json"
    add(ClipReviewQueue(path))
    document = json.loads(path.read_text())
    change(document["items"][0])
    path.write_text(json.dumps(document))
    with pytest.raises(ReviewQueueError):
        ClipReviewQueue(path)


def test_timing_update_preserves_candidate_and_note(tmp_path):
    queue = ClipReviewQueue(tmp_path / "reviews.json")
    original = queue.approve(add(queue)["review_id"], "keep")
    updated = queue.update_timing(
        original["review_id"], render_start=8, render_end=24,
        preview_path="/tmp/new.mp4", preview_metadata_path="/tmp/new.json",
    )
    assert updated["candidate_start"] == 10
    assert updated["candidate_end"] == 20
    assert updated["candidate_score"] == original["candidate_score"]
    assert updated["render_start"] == 8 and updated["render_end"] == 24
    assert updated["lead_in_seconds"] == 2 and updated["tail_seconds"] == 4
    assert updated["timing_revision"] == 1 and updated["timing_updated_at"]
    assert updated["status"] == "pending" and updated["reviewed_at"] is None
    assert updated["review_note"] == "keep"

    rerun_candidate = candidate(score=12)
    rerun_candidate.update(start=11, end=19, duration=8)
    rerun = add(queue, rerun_candidate)
    assert rerun["candidate_score"] == original["candidate_score"]
    assert rerun["candidate_start"] == 10 and rerun["candidate_end"] == 20
    assert rerun["render_start"] == 8 and rerun["render_end"] == 24
    assert rerun["status"] == "pending" and rerun["review_note"] == "keep"


def test_distinct_instances_serialize_concurrent_updates(tmp_path):
    path = tmp_path / "reviews.json"
    first_queue = ClipReviewQueue(path)
    first = add(first_queue)
    second = first_queue.add_or_update_preview(
        "video", candidate("c2", rank=2), "/tmp/second.mp4", "/tmp/second.json"
    )
    barrier = threading.Barrier(2)
    errors = []

    def update(review_id, action):
        try:
            queue = ClipReviewQueue(path)
            barrier.wait()
            action(queue, review_id)
        except Exception as error:  # pragma: no cover - asserted below
            errors.append(error)

    threads = [
        threading.Thread(target=update, args=(first["review_id"], lambda q, rid: q.approve(rid))),
        threading.Thread(target=update, args=(second["review_id"], lambda q, rid: q.reject(rid))),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    statuses = {item["review_id"]: item["status"] for item in ClipReviewQueue(path).list_items()}
    assert statuses == {first["review_id"]: "approved", second["review_id"]: "rejected"}


def test_lock_is_released_after_exception_and_corrupt_queue_is_not_overwritten(tmp_path):
    path = tmp_path / "reviews.json"
    queue = ClipReviewQueue(path)
    item = add(queue)
    with pytest.raises(RuntimeError):
        with queue.locked():
            raise RuntimeError("boom")
    assert queue.approve(item["review_id"])["status"] == "approved"

    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ReviewQueueError):
        queue.reject(item["review_id"])
    assert path.read_text(encoding="utf-8") == "{broken"
    assert not list(tmp_path.glob(".reviews.*.tmp"))
