from backend.app import review_clips
from backend.services.clip_review_queue import ClipReviewQueue


def item(queue, text="<script>alert(1)</script>"):
    return queue.add_or_update_preview("v", {
        "candidate_id": "c", "rank": 1, "score": 75,
        "start": 1, "end": 4, "duration": 3, "text": text,
    }, queue.path.parent / "media & clip.mp4", queue.path.parent / "preview.json")


def test_list_show_actions_and_missing(tmp_path, capsys):
    path = tmp_path / "reviews.json"
    queue = ClipReviewQueue(path)
    review = item(queue)
    assert review_clips.main(["--review-queue-path", str(path), "list", "--status", "pending", "--limit", "1"]) == 0
    assert review["review_id"] in capsys.readouterr().out
    assert review_clips.main(["--review-queue-path", str(path), "show", review["review_id"]]) == 0
    assert "<script>" in capsys.readouterr().out
    assert review_clips.main(["--review-queue-path", str(path), "approve", review["review_id"], "--note", "yes"]) == 0
    assert ClipReviewQueue(path).find_by_review_id(review["review_id"])["status"] == "approved"
    assert review_clips.main(["--review-queue-path", str(path), "pending", review["review_id"], "--clear-note"]) == 0
    pending = ClipReviewQueue(path).find_by_review_id(review["review_id"])
    assert pending["status"] == "pending" and pending["review_note"] is None
    assert review_clips.main(["--review-queue-path", str(path), "reject", "missing"]) == 1


def test_index_escaping_relative_paths_and_empty(tmp_path):
    path = tmp_path / "reviews.json"
    queue = ClipReviewQueue(path)
    item(queue)
    output = tmp_path / "index.html"
    assert review_clips.main([
        "--review-queue-path", str(path), "build-index", "--output-path", str(output)
    ]) == 0
    document = output.read_text()
    assert "&lt;script&gt;" in document and "<script>" not in document
    assert "media%20%26%20clip.mp4" in document
    empty = tmp_path / "empty.json"
    empty_index = tmp_path / "empty.html"
    assert review_clips.main([
        "--review-queue-path", str(empty), "build-index", "--output-path", str(empty_index)
    ]) == 0
    assert empty_index.read_text().count("No clips.") == 3


def test_list_show_and_index_display_adjusted_timing(tmp_path, capsys):
    path = tmp_path / "reviews.json"
    queue = ClipReviewQueue(path)
    review = item(queue)
    queue.update_timing(
        review["review_id"], render_start=0, render_end=6,
        preview_path=review["preview_path"],
        preview_metadata_path=review["preview_metadata_path"],
    )
    assert review_clips.main(["--review-queue-path", str(path), "list"]) == 0
    assert "ADJUSTED 6.00s" in capsys.readouterr().out
    assert review_clips.main([
        "--review-queue-path", str(path), "show", review["review_id"]
    ]) == 0
    shown = capsys.readouterr().out
    assert "Original candidate: 1.000-4.000" in shown
    assert "Current render: 0.000-6.000" in shown
    output = tmp_path / "index.html"
    review_clips.build_index(queue, output)
    document = output.read_text()
    assert "Timing adjusted" in document
    assert "Original candidate:" in document and "Render range:" in document
