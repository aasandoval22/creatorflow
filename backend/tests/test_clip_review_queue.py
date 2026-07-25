import json
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
    lambda d: d.update(version=2),
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
