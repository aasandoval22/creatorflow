from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.services.clip_review_queue import ClipReviewQueue
from backend.services.publication import (
    PublicationError, PublicationState, PublicationStore, sha256_file,
)


def candidate() -> dict:
    return {
        "candidate_id": "candidate_one", "rank": 1, "score": 88.0,
        "start": 1.0, "end": 11.0, "duration": 10.0,
        "text": "A complete reaction",
    }


def environment(tmp_path: Path):
    media = tmp_path / "preview.mp4"
    media.write_bytes(b"approved-render" * 100)
    queue = ClipReviewQueue(tmp_path / "reviews.json")
    item = queue.add_or_update_preview(
        "source_video", candidate(), media, tmp_path / "preview.json"
    )
    item = queue.approve(item["review_id"], "approved")
    store = PublicationStore(
        tmp_path / "publication.json", tmp_path / "publication.jsonl"
    )
    return queue, item, media, store


def prepare(store, item, media, **changes):
    values = {
        "review": item,
        "media_path": media,
        "media_sha256": sha256_file(media),
        "platform": "tiktok",
        "destination_account_id": "account-1",
        "destination_account_name": "Creator account",
        "caption": "Editable #caption",
        "source_attribution": "Source: Creator",
        "transport": "FILE_UPLOAD",
        "rights_confirmed": True,
    }
    values.update(changes)
    return store.prepare(**values)


def test_approval_alone_creates_no_publication(tmp_path):
    _, _, _, store = environment(tmp_path)
    assert store.list_attempts() == []
    assert not store.path.exists()


def test_rights_confirmation_and_approved_status_are_required(tmp_path):
    queue, item, media, store = environment(tmp_path)
    with pytest.raises(PublicationError, match="rights confirmation"):
        prepare(store, item, media, rights_confirmed=False)
    rejected = queue.reject(item["review_id"], "not suitable")
    with pytest.raises(PublicationError, match="Only an approved"):
        prepare(store, rejected, media)


def test_prepare_records_identity_consent_and_editable_caption(tmp_path):
    _, item, media, store = environment(tmp_path)
    attempt = prepare(store, item, media)
    assert attempt["state"] == "awaiting_consent"
    assert attempt["review_id"] == item["review_id"]
    assert attempt["source_video_id"] == "source_video"
    assert attempt["candidate_id"] == "candidate_one"
    assert attempt["timing_revision"] == 0
    assert attempt["rendered_media_sha256"] == sha256_file(media)
    assert attempt["rights_confirmed_at"]
    updated = prepare(store, item, media, caption="Changed #caption")
    assert updated["attempt_id"] == attempt["attempt_id"]
    assert updated["caption"] == "Changed #caption"
    assert len(store.list_attempts()) == 1


def test_transition_history_preserves_lifecycle_detail(tmp_path):
    _, item, media, store = environment(tmp_path)
    attempt = prepare(store, item, media)
    states = [
        PublicationState.QUEUED,
        PublicationState.INITIALIZING,
        PublicationState.TRANSFERRING,
        PublicationState.PROCESSING,
        PublicationState.INBOX_DELIVERED,
        PublicationState.AWAITING_CREATOR_POST,
        PublicationState.PUBLISH_COMPLETE,
    ]
    for state in states:
        attempt = store.transition(
            attempt["attempt_id"], state,
            remote_publish_id="publish-1" if state is PublicationState.TRANSFERRING else None,
        )
    assert attempt["state"] == "publish_complete"
    assert attempt["publish_completed_at"]
    events = [json.loads(line) for line in store.audit_path.read_text().splitlines()]
    assert [event["state"] for event in events][-1] == "publish_complete"
    assert events[0]["rights_confirmed_at"]


def test_illegal_transition_is_rejected(tmp_path):
    _, item, media, store = environment(tmp_path)
    attempt = prepare(store, item, media)
    with pytest.raises(PublicationError, match="not allowed"):
        store.transition(attempt["attempt_id"], PublicationState.PUBLISH_COMPLETE)


def test_timing_revision_and_checksum_changes_are_refused(tmp_path):
    queue, item, media, store = environment(tmp_path)
    attempt = prepare(store, item, media)
    updated = queue.update_timing(
        item["review_id"], render_start=0, render_end=12,
        preview_path=media, preview_metadata_path=tmp_path / "preview.json",
    )
    updated = queue.approve(updated["review_id"])
    with pytest.raises(PublicationError, match="revision changed"):
        store.assert_fresh(attempt, updated)
    current = queue.find_by_review_id(item["review_id"])
    attempt2 = prepare(store, current, media)
    media.write_bytes(b"changed")
    with pytest.raises(PublicationError, match="checksum changed"):
        store.assert_fresh(attempt2, current)


def test_prepared_attempt_can_be_marked_stale_and_reprepared(tmp_path):
    _, item, media, store = environment(tmp_path)
    attempt = prepare(store, item, media)
    assert store.mark_stale(item["review_id"], "timing changed") == 1
    with pytest.raises(PublicationError, match="stale"):
        store.assert_fresh(store.get(attempt["attempt_id"]), item)
    refreshed = prepare(store, item, media)
    assert refreshed["stale"] is False


def test_duplicate_idempotency_survives_store_reload(tmp_path):
    _, item, media, store = environment(tmp_path)
    first = prepare(store, item, media)
    reloaded = PublicationStore(store.path, store.audit_path)
    second = prepare(reloaded, item, media)
    assert second["attempt_id"] == first["attempt_id"]


def test_sanitized_errors_and_urls_never_enter_audit(tmp_path):
    _, item, media, store = environment(tmp_path)
    attempt = prepare(store, item, media)
    attempt = store.transition(attempt["attempt_id"], PublicationState.QUEUED)
    failed = store.transition(
        attempt["attempt_id"], PublicationState.CANCELLED,
        error_reason="access_token=secret https://example.test/private",
    )
    assert "secret" not in failed["error_reason"]
    audit = store.audit_path.read_text()
    assert "secret" not in audit and "example.test" not in audit


def test_corrupt_publication_state_is_actionable(tmp_path):
    path = tmp_path / "publication.json"
    path.write_text("{broken")
    with pytest.raises(PublicationError, match="corrupt"):
        PublicationStore(path, tmp_path / "audit.jsonl")
