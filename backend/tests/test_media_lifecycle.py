from __future__ import annotations

import json
from io import StringIO
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.app import media_cleanup
from backend.services.clip_review_queue import ClipReviewQueue
from backend.services.media_lifecycle import (
    MediaCleanupService, MediaLifecycleError, MediaOwnershipGraph, RetentionPolicy,
)
from backend.services.publication import PublicationState, PublicationStore, sha256_file
from backend.services.video_manifest import (
    VideoManifest, VideoStatus, default_clip_analysis, default_transcription,
)


NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)


class NoReferences:
    def __init__(self, path: Path, media=()):
        self.index_path = path
        self.media = list(media)
        if self.media:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}")

    def list_references(self):
        return [{"media_path": str(path)} for path in self.media]


def policy(*, quarantine=0):
    return RetentionPolicy(
        source_media_retention_days=0,
        rejected_preview_retention_days=0,
        published_media_retention_days=0,
        quarantine_retention_days=quarantine,
    )


def setup(tmp_path: Path, *, decision="rejected", references=()):
    root = tmp_path / "data"
    source = root / "downloads" / "creator" / "video.mp4"
    preview = root / "previews" / "video" / "candidate.mp4"
    source.parent.mkdir(parents=True)
    preview.parent.mkdir(parents=True)
    source.write_bytes(b"source" * 100)
    preview.write_bytes(b"preview" * 100)
    manifest = VideoManifest(root / "manifests" / "videos.json")
    record = {
        "video_id": "video", "source_platform": "youtube",
        "channel_name": "Creator", "channel_url": "https://youtube.test/@creator",
        "video_url": "https://youtube.test/watch?v=video", "title": "Title",
        "uploader": "Creator", "upload_date": "20250101",
        "duration_seconds": 60, "discovered_at": "2025-01-01T00:00:00+00:00",
        "downloaded_at": "2025-01-01T00:01:00+00:00",
        "local_file_path": str(source), "status": VideoStatus.DOWNLOADED.value,
        "error_message": None, "transcription": default_transcription(),
        "clip_analysis": default_clip_analysis(),
    }
    manifest.upsert(record)
    queue = ClipReviewQueue(root / "review_queue" / "reviews.json")
    candidate = {
        "candidate_id": "candidate", "rank": 1, "score": 90,
        "start": 5.0, "end": 20.0, "duration": 15.0, "text": "complete beat",
    }
    review = queue.add_or_update_preview(
        "video", candidate, preview, preview.with_suffix(".json")
    )
    if decision == "rejected":
        review = queue.reject(review["review_id"], "not selected")
    elif decision == "approved":
        review = queue.approve(review["review_id"], "selected")
    publications = PublicationStore(
        root / "publication" / "records.json",
        root / "publication" / "events.jsonl",
    )
    reference_store = NoReferences(
        root / "reference_clips" / "index.json", references
    )
    graph = MediaOwnershipGraph(
        data_root=root, manifest=manifest, queue=queue,
        publications=publications, references=reference_store,
    )
    service = MediaCleanupService(
        graph, policy=policy(), now=lambda: NOW,
        production_lock_factory=lambda: nullcontext(),
    )
    return service, source, preview, review


def items(service):
    return {item["kind"]: item for item in service.evaluate()}


def publish(service, review, preview):
    store = service.graph.publications
    attempt = store.prepare(
        review=review, media_path=preview, media_sha256=sha256_file(preview),
        platform="tiktok", destination_account_id="account",
        destination_account_name="Account", caption="caption",
        source_attribution="Source: Creator", transport="FILE_UPLOAD",
        rights_confirmed=True,
    )
    for state in (
        PublicationState.QUEUED, PublicationState.INITIALIZING,
        PublicationState.TRANSFERRING, PublicationState.PROCESSING,
        PublicationState.PUBLISH_COMPLETE,
    ):
        attempt = store.transition(
            attempt["attempt_id"], state,
            remote_publish_id="publish-id" if state is PublicationState.TRANSFERRING else None,
        )
    return attempt


def test_ownership_graph_maps_source_review_and_artifacts(tmp_path):
    service, source, preview, review = setup(tmp_path)
    graph = service.ownership()
    source_node = graph["sources"][0]
    assert source_node["source_creator"] == "Creator"
    assert source_node["source_video_id"] == "video"
    assert source_node["downloaded_source_media"] == str(source)
    assert source_node["reviews"][0]["review_id"] == review["review_id"]
    assert source_node["reviews"][0]["rendered_preview"] == str(preview)
    assert "publication records and audits" in graph["ownership"]["authoritative"]
    assert "metadata" in graph["ownership"]["always_retained"]


def test_rejected_dependencies_become_eligible_after_retention(tmp_path):
    service, _, _, _ = setup(tmp_path)
    evaluated = items(service)
    assert evaluated["source_media"]["eligible"]
    assert evaluated["rendered_preview"]["eligible"]


def test_rejected_preview_waits_full_retention_period(tmp_path):
    service, _, _, review = setup(tmp_path)
    service.policy = RetentionPolicy(
        source_media_retention_days=7,
        rejected_preview_retention_days=14,
        published_media_retention_days=30,
        quarantine_retention_days=7,
    )
    reviewed = datetime.fromisoformat(review["reviewed_at"])
    service.now = lambda: reviewed + timedelta(days=13)
    assert not items(service)["rendered_preview"]["eligible"]
    service.now = lambda: reviewed + timedelta(days=15)
    assert items(service)["rendered_preview"]["eligible"]


def test_approved_unpublished_source_and_render_are_retained(tmp_path):
    service, _, _, _ = setup(tmp_path, decision="approved")
    evaluated = items(service)
    assert not evaluated["source_media"]["eligible"]
    assert "lacks verified publication" in " ".join(evaluated["source_media"]["reasons"])
    assert not evaluated["rendered_preview"]["eligible"]
    assert "unpublished" in " ".join(evaluated["rendered_preview"]["reasons"])


def test_verified_publish_complete_enables_retention_calculation(tmp_path):
    service, _, preview, review = setup(tmp_path, decision="approved")
    publish(service, review, preview)
    evaluated = items(service)
    assert evaluated["source_media"]["eligible"]
    assert evaluated["rendered_preview"]["eligible"]


def test_published_render_waits_full_retention_period(tmp_path):
    service, _, preview, review = setup(tmp_path, decision="approved")
    completed = publish(service, review, preview)
    service.policy = RetentionPolicy(
        source_media_retention_days=7,
        rejected_preview_retention_days=14,
        published_media_retention_days=30,
        quarantine_retention_days=7,
    )
    published = datetime.fromisoformat(completed["publish_completed_at"])
    service.now = lambda: published + timedelta(days=29)
    assert not items(service)["rendered_preview"]["eligible"]
    service.now = lambda: published + timedelta(days=31)
    assert items(service)["rendered_preview"]["eligible"]


def test_failed_and_processing_publications_block_cleanup(tmp_path):
    service, _, preview, review = setup(tmp_path, decision="approved")
    attempt = service.graph.publications.prepare(
        review=review, media_path=preview, media_sha256=sha256_file(preview),
        platform="tiktok", destination_account_id="account",
        destination_account_name="Account", caption="caption",
        source_attribution="source", transport="FILE_UPLOAD", rights_confirmed=True,
    )
    attempt = service.graph.publications.transition(
        attempt["attempt_id"], PublicationState.QUEUED
    )
    assert not items(service)["rendered_preview"]["eligible"]
    attempt = service.graph.publications.transition(
        attempt["attempt_id"], PublicationState.INITIALIZING
    )
    service.graph.publications.transition(
        attempt["attempt_id"], PublicationState.FAILED_TERMINAL,
        error_reason="platform rejected the request",
    )
    blocked = items(service)["rendered_preview"]
    assert not blocked["eligible"]
    assert "failed publication" in " ".join(blocked["reasons"])


def test_reference_and_recovery_sources_are_excluded(tmp_path):
    service, source, _, _ = setup(tmp_path)
    service.graph.references = NoReferences(
        service.data_root / "reference_clips" / "index.json", [source]
    )
    assert "accepted reference" in " ".join(items(service)["source_media"]["reasons"])
    service.graph.references = NoReferences(
        service.data_root / "reference_clips" / "other-index.json"
    )
    recovery = service.data_root / "reference_evidence_recovery" / "operation.json"
    recovery.parent.mkdir(parents=True)
    recovery.write_text(json.dumps({"source_video_id": "video"}))
    assert "recovery operation" in " ".join(items(service)["source_media"]["reasons"])


def test_active_comparison_blocks_source_and_preview(tmp_path):
    service, _, _, review = setup(tmp_path)
    batch = service.data_root / "review_comparison_batches" / "batch_active"
    batch.mkdir(parents=True)
    (batch / "manifest.json").write_text(json.dumps({
        "items": [{"review_id": review["review_id"]}],
    }))
    evaluated = items(service)
    assert "active comparison" in " ".join(evaluated["source_media"]["reasons"])
    assert "active comparison" in " ".join(evaluated["rendered_preview"]["reasons"])


def test_cleanup_plan_is_checksum_pinned_and_tamper_evident(tmp_path):
    service, _, _, _ = setup(tmp_path)
    plan = service.plan()
    assert plan["content_sha256"] and service.show(plan["plan_id"]) == plan
    path = service.plan_path(plan["plan_id"])
    changed = json.loads(path.read_text())
    changed["items"][0]["size_bytes"] += 1
    path.write_text(json.dumps(changed))
    with pytest.raises(MediaLifecycleError, match="checksum"):
        service.show(plan["plan_id"])


def test_state_change_after_plan_skips_item_without_moving_it(tmp_path):
    service, source, preview, review = setup(tmp_path)
    plan = service.plan()
    service.graph.queue.return_to_pending(review["review_id"])
    result = service.apply(plan["plan_id"], confirm=plan["plan_id"], execute=True)
    assert source.exists() and preview.exists()
    assert {item["status"] for item in result["items"]} == {"skipped"}


def test_symlink_and_outside_root_are_never_eligible(tmp_path):
    service, source, _, _ = setup(tmp_path)
    target = source.with_name("real.mp4")
    source.replace(target)
    source.symlink_to(target)
    source_item = items(service)["source_media"]
    assert not source_item["eligible"]
    assert "symbolic" in " ".join(source_item["reasons"])


def test_invalid_source_does_not_block_unrelated_preview_quarantine(tmp_path):
    service, source, preview, _ = setup(tmp_path)
    target = source.with_name("real.mp4")
    source.replace(target)
    source.symlink_to(target)
    plan = service.plan()
    result = service.apply(
        plan["plan_id"], confirm=plan["plan_id"], execute=True
    )
    assert source.is_symlink()
    assert not preview.exists()
    assert [item["relative_path"] for item in result["items"]] == [
        "previews/video/candidate.mp4"
    ]


def test_unexpected_file_ownership_is_rejected(tmp_path):
    service, _, _, _ = setup(tmp_path)
    service.owner_uid += 1
    assert all(not item["eligible"] for item in service.evaluate())
    assert all(
        "ownership" in " ".join(item["reasons"])
        for item in service.evaluate()
    )


def test_media_path_outside_persistent_root_is_rejected(tmp_path):
    service, _, _, _ = setup(tmp_path)
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")
    record = service.graph.manifest.get("video")
    record["local_file_path"] = str(outside)
    service.graph.manifest.upsert(record)
    source = items(service)["source_media"]
    assert not source["eligible"]
    assert source["relative_path"] == "<outside-root>"
    assert "escapes" in " ".join(source["reasons"])


def test_quarantine_restore_and_idempotent_apply(tmp_path):
    service, source, preview, _ = setup(tmp_path)
    plan = service.plan()
    dry = service.apply(plan["plan_id"], confirm=plan["plan_id"])
    assert dry["status"] == "dry_run" and source.exists() and preview.exists()
    quarantined = service.apply(
        plan["plan_id"], confirm=plan["plan_id"], execute=True
    )
    assert not source.exists() and not preview.exists()
    assert quarantined["quarantined_bytes"] > 0
    repeated = service.apply(
        plan["plan_id"], confirm=plan["plan_id"], execute=True
    )
    assert repeated["quarantine_id"] == quarantined["quarantine_id"]
    restored = service.restore(quarantined["quarantine_id"])
    assert restored["state"] == "restored" and source.exists() and preview.exists()
    assert service.restore(quarantined["quarantine_id"])["state"] == "restored"


def test_interrupted_quarantine_recovers_atomic_move(tmp_path):
    service, _, _, _ = setup(tmp_path)
    plan = service.plan()
    quarantined = service.apply(
        plan["plan_id"], confirm=plan["plan_id"], execute=True
    )
    manifest_path = (
        service.quarantine_path(quarantined["quarantine_id"]) / "manifest.json"
    )
    interrupted = json.loads(manifest_path.read_text())
    missing_record = interrupted["items"].pop()
    interrupted["quarantined_bytes"] -= missing_record["size_bytes"]
    interrupted["state"] = "quarantining"
    manifest_path.write_text(json.dumps(interrupted))
    resumed = service.apply(
        plan["plan_id"], confirm=plan["plan_id"], execute=True
    )
    assert resumed["state"] == "quarantined"
    assert len(resumed["items"]) == 2
    recovered = next(
        item for item in resumed["items"]
        if item["relative_path"] == missing_record["relative_path"]
    )
    assert recovered["status"] == "quarantined"
    assert "interrupted atomic move" in recovered["detail"]


def test_purge_requires_grace_and_separate_confirmation(tmp_path):
    service, _, _, _ = setup(tmp_path)
    service.policy = policy(quarantine=7)
    plan = service.plan()
    quarantined = service.apply(
        plan["plan_id"], confirm=plan["plan_id"], execute=True
    )
    with pytest.raises(MediaLifecycleError, match="confirmation"):
        service.purge(quarantined["quarantine_id"], confirm="wrong")
    with pytest.raises(MediaLifecycleError, match="blocked until"):
        service.purge(
            quarantined["quarantine_id"], confirm=quarantined["quarantine_id"]
        )
    service.now = lambda: NOW + timedelta(days=8)
    purged = service.purge(
        quarantined["quarantine_id"], confirm=quarantined["quarantine_id"]
    )
    assert purged["state"] == "purged" and purged["purged_bytes"] > 0
    assert service.purge(
        quarantined["quarantine_id"], confirm=quarantined["quarantine_id"]
    )["state"] == "purged"


def test_cleanup_audit_is_sanitized_and_contains_no_file_contents(tmp_path):
    service, _, _, _ = setup(tmp_path)
    service._audit("token=private-value https://example.test/private", contents="bytes")
    audit = service.audit_path.read_text()
    assert "private-value" not in audit and "example.test" not in audit
    assert "contents" not in audit


def test_cleanup_cli_plan_and_default_apply_are_non_destructive(tmp_path):
    service, source, preview, _ = setup(tmp_path)
    stream = StringIO()
    assert media_cleanup.main(["plan"], service=service, stdout=stream) == 0
    output = stream.getvalue()
    assert "ELIGIBLE" in output and "Planning does not move" in output
    plan_id = next(service.plan_root.glob("plan_*.json")).stem
    assert media_cleanup.main(
        ["apply", "--plan-id", plan_id, "--confirm", plan_id],
        service=service,
    ) == 0
    assert source.exists() and preview.exists()
