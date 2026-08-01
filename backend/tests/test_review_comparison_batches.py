from __future__ import annotations

import hashlib
import json

from backend.app import review_comparisons, review_server
from backend.services.clip_review_queue import ClipReviewQueue
from backend.services.reference_clip_comparator import ReferenceClipComparator
from backend.services.reference_profile_builder import ReferenceProfileBuilder
from backend.services.review_comparison_batches import ReviewComparisonBatchService
from backend.tests.test_reference_clips import analyze_reference


def candidate(identifier: str, start: float = 1, end: float = 8) -> dict:
    return {
        "candidate_id": identifier, "rank": 1, "score": 80,
        "start": start, "end": end, "duration": end - start,
        "text": "What? because finally!",
    }


def setup(tmp_path):
    library, _, _ = analyze_reference(tmp_path / "reference")
    builder = ReferenceProfileBuilder(library, tmp_path / "profiles")
    builder.build("personality_reaction")
    queue = ClipReviewQueue(tmp_path / "reviews.json")
    transcript = tmp_path / "transcript.json"
    transcript.write_text(json.dumps({"segments": [{"words": [
        {"word": "What?", "start": 1.1, "end": 1.4},
        {"word": "because", "start": 3.0, "end": 3.4},
        {"word": "finally!", "start": 7.0, "end": 7.4},
    ]}]}), encoding="utf-8")
    metadata = tmp_path / "preview.json"
    metadata.write_text(json.dumps({
        "source_transcript_path": str(transcript),
        "render_start": 1.0, "render_end": 8.0,
        "scene_change_timestamps": [2, 4], "silence_intervals": [],
        "caption_configuration": {"enabled": True},
    }), encoding="utf-8")
    item = queue.add_or_update_preview(
        "video-one", candidate("one"), tmp_path / "preview.mp4", metadata,
    )
    comparator = ReferenceClipComparator(builder, tmp_path / "legacy")
    service = ReviewComparisonBatchService(
        queue, builder, comparator, root=tmp_path / "batches",
    )
    return queue, builder, service, item


def test_stable_batch_pins_membership_profile_and_is_read_only(tmp_path):
    queue, builder, service, item = setup(tmp_path)
    queue_before_capture = queue.path.read_bytes()
    manifest = service.capture("personality_reaction")
    assert queue.path.read_bytes() == queue_before_capture
    assert [value["review_id"] for value in manifest["items"]] == [item["review_id"]]
    pinned = service.path(manifest["batch_id"]) / "profile.json"
    assert hashlib.sha256(pinned.read_bytes()).hexdigest() == manifest["profile_sha256"]
    # Queue membership and profile can move after capture without changing the batch.
    queue.add_or_update_preview(
        "video-two", candidate("two"), tmp_path / "two.mp4", tmp_path / "missing.json"
    )
    queue.reject(item["review_id"], "fixture decision")
    profile_path = builder.profile_path("personality_reaction")
    current_profile = json.loads(profile_path.read_text(encoding="utf-8"))
    current_profile["duration"]["recommended_target"] = 999
    profile_path.write_text(json.dumps(current_profile), encoding="utf-8")
    queue_before_run = queue.path.read_bytes()
    summary = service.run(manifest["batch_id"])
    assert queue.path.read_bytes() == queue_before_run
    assert summary["item_count"] == 1
    assert summary["reports"][0]["status_or_revision_changed"] is True
    report_path = service.path(manifest["batch_id"]) / "reports" / f"{item['review_id']}.json"
    report_before = report_path.read_bytes()
    assert json.loads(report_before)["batch"]["profile_sha256"] == manifest["profile_sha256"]
    assert service.run(manifest["batch_id"]) == summary
    assert report_path.read_bytes() == report_before
    report = json.loads(report_before)
    assert all(finding["status"] != "defective" for finding in report["findings"].values())
    html = review_server._comparison_section(report)
    assert manifest["batch_id"] in html and "Status/revision changed since capture" in html


def test_batch_cli_explicit_argv(tmp_path, capsys):
    queue, builder, service, _ = setup(tmp_path)
    common = [
        "--review-queue-path", str(queue.path),
        "--reference-root", str(builder.library.root),
        "--index-path", str(builder.library.index_path),
        "--profile-directory", str(builder.output_directory),
        "--annotation-directory", str(builder.annotation_store.root),
        "--batch-directory", str(service.root),
    ]
    assert review_comparisons.main([*common, "capture", "--profile", "personality_reaction"]) == 0
    batch_id = capsys.readouterr().out.split("Captured batch: ", 1)[1].splitlines()[0]
    assert review_comparisons.main([*common, "run", "--batch-id", batch_id]) == 0
    assert review_comparisons.main([*common, "show", "--batch-id", batch_id]) == 0
