from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.services.reference_clip_library import ReferenceClipLibrary
from backend.services.reference_decision_audit import (
    EVENT_FIELDS,
    ReferenceDecisionAuditError,
    ReferenceDecisionAuditLedger,
)
from backend.services.reference_discovery import (
    ReferenceCandidateQueue,
    ReferenceDiscoveryError,
    ReferenceDiscoveryService,
    main,
)


class OfflineAPI:
    pass


class WritingAnalyzer:
    def __init__(self, library):
        self.library = library

    def analyze(self, reference_id, *, transcription):
        entry = self.library.get(reference_id)
        document = {
            "version": 1,
            "reference_id": reference_id,
            "created_at": "2026-07-25T00:00:02+00:00",
            "media": {"duration": 45.0},
            "speech": {},
            "visual_timing": {},
            "audio_timing": {},
            "annotations": {},
        }
        Path(entry["analysis_path"]).write_text(
            json.dumps(document) + "\n", encoding="utf-8"
        )
        return document


class FailingLedger(ReferenceDecisionAuditLedger):
    def append(self, event):
        raise ReferenceDecisionAuditError("synthetic audit failure")


def candidate(video_id="one", *, media_path=None):
    return {
        "video_id": video_id,
        "source_url": f"https://www.youtube.com/watch?v={video_id}",
        "title": f"Gaming candidate {video_id}",
        "creator": f"Creator {video_id}",
        "channel_id": f"channel-{video_id}",
        "channel_title": f"Creator {video_id}",
        "description": "gaming",
        "tags": ["gaming"],
        "category_id": "20",
        "published_at": "2026-07-20T00:00:00Z",
        "view_count": 1000,
        "like_count": 100,
        "comment_count": 10,
        "duration": 45,
        "verified_duration": 45,
        "verified_vertical": True,
        "width": 1080,
        "height": 1920,
        "frame_rate": 60.0,
        "has_video": True,
        "has_audio": True,
        "downloadable": True,
        "media_path": str(media_path) if media_path else None,
        "validation_error": None,
        "validation_status": "media-verified",
        "media_verification": "verified",
        "validation_evidence": "fixture verified",
        "discovery_query": "gaming shorts",
        "captured_at": "2026-07-25T00:00:00Z",
        "topic": "gaming",
        "cohort": "established",
        "ranking": {"total": 1, "evidence": "fixture"},
        "score": 1,
        "rank": 1,
    }


def environment(tmp_path, *, video_ids=("one",), ledger_class=ReferenceDecisionAuditLedger):
    data_root = tmp_path / "data"
    queue = ReferenceCandidateQueue(
        data_root / "reference_discovery" / "candidates.json",
        data_root=data_root,
    )
    for video_id in video_ids:
        media = data_root / "reference_discovery" / "media" / f"{video_id}.mp4"
        media.parent.mkdir(parents=True, exist_ok=True)
        media.write_bytes(f"retained-{video_id}".encode())
        queue.upsert_discovered([candidate(video_id, media_path=media)])
    references = data_root / "reference_clips"
    library = ReferenceClipLibrary(references)
    audit = ledger_class(data_root / "reference_discovery" / "decision_events.jsonl")
    service = ReferenceDiscoveryService(
        OfflineAPI(),
        queue,
        reference_library=library,
        analyzer_factory=WritingAnalyzer,
        reference_root=references,
        profile_root=data_root / "reference_profiles",
        audit_ledger=audit,
        reviewer_name="Local reviewer",
    )
    return data_root, queue, library, audit, service


def accept(service, queue, video_id="one", *, note=""):
    return service.accept(
        video_id,
        category="gaming_highlight",
        notes=note,
        expected_revision=queue.get(video_id)["revision"],
        request_id=f"accept-{video_id}",
    )


def force_legacy_status(queue, video_id, status, **kwargs):
    current = queue.get(video_id)
    return queue.transition(
        video_id,
        expected_revision=current["revision"],
        expected_status=current["status"],
        status=status,
        **kwargs,
    )


def test_allowed_reject_duplicate_and_reconsider_transitions(tmp_path):
    _, queue, _, audit, service = environment(tmp_path)
    rejected = service.transition(
        "one", "reject", notes="Weak story beat",
        expected_revision=0, request_id="reject-one",
    )
    assert rejected["status"] == "rejected" and rejected["revision"] == 1
    reconsidered = service.transition(
        "one", "reconsider", notes=None,
        expected_revision=1, request_id="reconsider-one",
    )
    assert reconsidered["status"] == "discovered"
    duplicate = service.transition(
        "one", "duplicate", notes="Same source as another clip",
        expected_revision=2, request_id="duplicate-one",
    )
    assert duplicate["status"] == "duplicate"
    final = service.transition(
        "one", "reconsider", notes=None,
        expected_revision=3, request_id="reconsider-two",
    )
    assert final["status"] == "discovered" and final["revision"] == 4
    assert [event["action"] for event in audit.history("one")] == [
        "reject", "reconsider", "duplicate", "reconsider"
    ]


@pytest.mark.parametrize(
    ("starting", "action"),
    [
        ("discovered", "reconsider"),
        ("rejected", "duplicate"),
        ("duplicate", "reject"),
        ("accepted", "reject"),
        ("accepted", "reconsider"),
    ],
)
def test_forbidden_transitions_change_nothing(tmp_path, starting, action):
    _, queue, library, audit, service = environment(tmp_path)
    if starting == "accepted":
        accept(service, queue)
    elif starting == "rejected":
        queue.decide("one", "rejected", notes="setup")
    elif starting == "duplicate":
        queue.decide("one", "duplicate", notes="setup")
    before_queue = queue.path.read_bytes()
    before_index = library.index_path.read_bytes() if library.index_path.exists() else None
    before_audit = audit.path.read_bytes() if audit.path.exists() else None
    with pytest.raises(ReferenceDiscoveryError, match="Cannot"):
        service.transition(
            "one", action, notes="Reason",
            expected_revision=queue.get("one")["revision"],
            request_id="invalid-transition",
        )
    assert queue.path.read_bytes() == before_queue
    assert (
        library.index_path.read_bytes() if library.index_path.exists() else None
    ) == before_index
    assert (audit.path.read_bytes() if audit.path.exists() else None) == before_audit


def test_queue_compatibility_update_cannot_cross_accepted_boundary(tmp_path):
    _, queue, _, audit, service = environment(tmp_path)
    accept(service, queue)
    before = queue.path.read_bytes()
    with pytest.raises(ReferenceDiscoveryError, match="decision service"):
        queue.decide("one", "rejected", notes="bypass attempt")
    assert queue.path.read_bytes() == before
    assert len(audit.history("one")) == 1


def test_stale_and_replayed_requests_do_not_mutate_or_audit(tmp_path):
    _, queue, _, audit, service = environment(tmp_path)
    service.transition(
        "one", "reject", notes="Not suitable",
        expected_revision=0, request_id="first",
    )
    before_queue = queue.path.read_bytes()
    before_audit = audit.path.read_bytes()
    with pytest.raises(ReferenceDiscoveryError, match="already processed"):
        service.transition(
            "one", "reject", notes="Replay",
            expected_revision=0, request_id="first",
        )
    assert queue.path.read_bytes() == before_queue
    assert audit.path.read_bytes() == before_audit
    with pytest.raises(ReferenceDiscoveryError, match="Stale"):
        service.transition(
            "one", "reconsider", notes=None,
            expected_revision=0, request_id="stale-new-request",
        )
    assert queue.path.read_bytes() == before_queue
    assert audit.path.read_bytes() == before_audit


def test_legacy_candidate_without_revision_starts_at_zero(tmp_path):
    data_root, queue, _, audit, service = environment(tmp_path)
    document = json.loads(queue.path.read_text())
    document["items"][0].pop("revision")
    queue.path.write_text(json.dumps(document) + "\n", encoding="utf-8")
    assert queue.get("one")["revision"] == 0
    updated = service.transition(
        "one", "reject", notes="Not a fit",
        expected_revision=0, request_id="legacy-first-decision",
    )
    assert updated["revision"] == 1
    assert json.loads(queue.path.read_text())["items"][0]["revision"] == 1
    assert audit.history("one")[0]["previous_revision"] == 0


@pytest.mark.parametrize("action", ["reject", "duplicate"])
def test_negative_decisions_require_meaningful_notes(tmp_path, action):
    _, queue, _, audit, service = environment(tmp_path)
    before = queue.path.read_bytes()
    with pytest.raises(ReferenceDiscoveryError, match="meaningful"):
        service.transition(
            "one", action, notes="   ", expected_revision=0,
            request_id=f"{action}-without-note",
        )
    assert queue.path.read_bytes() == before
    assert not audit.path.exists()


def test_acceptance_note_is_optional_and_event_is_sanitized(tmp_path):
    _, queue, library, audit, service = environment(tmp_path)
    accept(service, queue, note="")
    item = queue.get("one")
    assert item["status"] == "accepted" and item["revision"] == 1
    assert library.get("youtube-one")["status"] == "accepted"
    event = audit.history("one")[0]
    assert set(event) == EVENT_FIELDS
    assert event["reviewer"] == "Local reviewer"
    assert event["note"] is None
    serialized = json.dumps(event).lower()
    for secret_name in ("form_token", "authorization", "cookie", "api_key"):
        assert secret_name not in serialized


def test_acceptance_rolls_back_when_audit_append_fails(tmp_path):
    _, queue, library, audit, service = environment(
        tmp_path, ledger_class=FailingLedger
    )
    before = queue.path.read_bytes()
    with pytest.raises(ReferenceDecisionAuditError, match="synthetic"):
        accept(service, queue)
    assert queue.path.read_bytes() == before
    assert library.list_references() == []
    assert not (service.reference_root / "discovered-one").exists()
    assert not audit.path.exists()


def test_successful_withdrawal_preserves_candidate_media_and_metadata(tmp_path):
    data_root, queue, library, audit, service = environment(
        tmp_path, video_ids=("one", "two")
    )
    accept(service, queue, "one")
    accept(service, queue, "two")
    before = queue.get("one")
    unrelated = library.get("youtube-two")
    retained = queue.resolve_media_path(before["media_path"])
    accepted_directory = Path(library.get("youtube-one")["media_path"]).parent
    result = service.withdraw(
        "one",
        status="rejected",
        notes="Does not match the desired clip style",
        expected_revision=before["revision"],
        confirmed=True,
        request_id="withdraw-one",
    )
    after = result["candidate"]
    assert after["status"] == "rejected"
    assert after["accepted_reference_id"] is None
    assert after["revision"] == before["revision"] + 1
    for field in (
        "source_url", "creator", "topic", "cohort", "ranking",
        "media_path", "created_at",
    ):
        assert after[field] == before[field]
    assert retained.is_file()
    assert not accepted_directory.exists()
    recovery = data_root / "reference_discovery" / result["recovery_key"]
    assert (recovery / "reference.mp4").is_file()
    with pytest.raises(Exception):
        library.get("youtube-one")
    assert library.get("youtube-two") == unrelated
    event = audit.history("one")[-1]
    assert event["action"] == "withdraw"
    assert event["accepted_reference_id_before"] == "youtube-one"
    assert event["accepted_reference_id_after"] is None


def test_repeated_withdrawal_is_nonmutating(tmp_path):
    _, queue, library, audit, service = environment(tmp_path)
    accept(service, queue)
    service.withdraw(
        "one", notes="Not a fit", expected_revision=1,
        confirmed=True, request_id="withdraw-once",
    )
    before_queue = queue.path.read_bytes()
    before_index = library.index_path.read_bytes()
    before_audit = audit.path.read_bytes()
    with pytest.raises(ReferenceDiscoveryError, match="Cannot withdraw"):
        service.withdraw(
            "one", notes="Replay", expected_revision=2,
            confirmed=True, request_id="withdraw-twice",
        )
    assert queue.path.read_bytes() == before_queue
    assert library.index_path.read_bytes() == before_index
    assert audit.path.read_bytes() == before_audit


def test_withdrawal_repairs_rejected_candidate_with_owned_reference(tmp_path):
    _, queue, library, audit, service = environment(tmp_path)
    accept(service, queue)
    force_legacy_status(
        queue, "one", "rejected", notes="legacy rejection"
    )
    before = queue.get("one")
    result = service.withdraw(
        "one", notes="Does not match desired style",
        expected_revision=before["revision"], confirmed=True,
        request_id="repair-withdrawal",
    )
    assert result["candidate"]["status"] == "rejected"
    assert result["candidate"]["accepted_reference_id"] is None
    assert result["candidate"]["revision"] == before["revision"] + 1
    assert library.list_references() == []
    event = audit.history("one")[-1]
    assert event["previous_status"] == "rejected"
    assert event["requested_status"] == "rejected"
    assert event["action"] == "withdraw"


def test_withdrawal_requires_confirmation_and_note(tmp_path):
    _, queue, _, _, service = environment(tmp_path)
    accept(service, queue)
    with pytest.raises(ReferenceDiscoveryError, match="confirmation"):
        service.withdraw(
            "one", notes="Reason", expected_revision=1, confirmed=False
        )
    with pytest.raises(ReferenceDiscoveryError, match="meaningful"):
        service.withdraw(
            "one", notes="", expected_revision=1, confirmed=True
        )


def test_withdrawal_refuses_profile_in_use(tmp_path):
    data_root, queue, library, audit, service = environment(tmp_path)
    accept(service, queue)
    profile = data_root / "reference_profiles" / "gaming_highlight.json"
    profile.parent.mkdir(parents=True)
    profile.write_text(
        json.dumps(
            {
                "version": 1,
                "profile_name": "gaming_highlight",
                "reference_ids": ["youtube-one"],
            }
        ),
        encoding="utf-8",
    )
    before = (queue.path.read_bytes(), library.index_path.read_bytes(), audit.path.read_bytes())
    with pytest.raises(ReferenceDiscoveryError, match="used by profile"):
        service.withdraw(
            "one", notes="No longer desired", expected_revision=1,
            confirmed=True, request_id="profile-block",
        )
    assert before == (
        queue.path.read_bytes(), library.index_path.read_bytes(), audit.path.read_bytes()
    )


def test_withdrawal_refuses_bad_checksum_and_wrong_ownership(tmp_path):
    _, queue, library, audit, service = environment(tmp_path)
    accept(service, queue)
    media = Path(library.get("youtube-one")["media_path"])
    media.write_bytes(b"changed")
    before = (queue.path.read_bytes(), library.index_path.read_bytes(), audit.path.read_bytes())
    with pytest.raises(ReferenceDiscoveryError, match="checksum"):
        service.withdraw(
            "one", notes="Reason", expected_revision=1,
            confirmed=True, request_id="bad-checksum",
        )
    assert before == (
        queue.path.read_bytes(), library.index_path.read_bytes(), audit.path.read_bytes()
    )
    media.write_bytes(b"retained-one")
    queue.decide("one", "accepted", accepted_reference_id="youtube-other")
    before_audit = audit.path.read_bytes()
    with pytest.raises(ReferenceDiscoveryError, match="does not own"):
        service.withdraw(
            "one", notes="Reason",
            expected_revision=queue.get("one")["revision"],
            confirmed=True, request_id="wrong-owner",
        )
    assert audit.path.read_bytes() == before_audit


def test_withdrawal_refuses_symlinked_reference_directory(tmp_path):
    _, queue, library, audit, service = environment(tmp_path)
    accept(service, queue)
    directory = Path(library.get("youtube-one")["media_path"]).parent
    external = tmp_path / "external-reference"
    directory.rename(external)
    directory.symlink_to(external, target_is_directory=True)
    before = (
        queue.path.read_bytes(),
        library.index_path.read_bytes(),
        audit.path.read_bytes(),
    )
    with pytest.raises(ReferenceDiscoveryError, match="symbolic link"):
        service.withdraw(
            "one", notes="Reason", expected_revision=1,
            confirmed=True, request_id="symlinked-reference",
        )
    assert before == (
        queue.path.read_bytes(),
        library.index_path.read_bytes(),
        audit.path.read_bytes(),
    )
    assert directory.is_symlink()
    assert (external / "reference.mp4").is_file()


@pytest.mark.parametrize("failure_point", ["move", "index", "queue", "audit"])
def test_withdrawal_rolls_back_injected_failures(
    tmp_path, monkeypatch, failure_point
):
    _, queue, library, audit, service = environment(tmp_path)
    accept(service, queue)
    before_queue = queue.path.read_bytes()
    before_index = library.index_path.read_bytes()
    accepted_directory = Path(library.get("youtube-one")["media_path"]).parent
    before_files = sorted(path.name for path in accepted_directory.iterdir())
    if failure_point == "move":
        service.move_path = lambda _source, _destination: (
            (_ for _ in ()).throw(OSError("synthetic move failure"))
        )
    elif failure_point == "index":
        monkeypatch.setattr(
            library, "remove",
            lambda _reference_id: (
                (_ for _ in ()).throw(OSError("synthetic index failure"))
            ),
        )
    elif failure_point == "queue":
        monkeypatch.setattr(
            queue, "transition",
            lambda *args, **kwargs: (
                (_ for _ in ()).throw(OSError("synthetic queue failure"))
            ),
        )
    else:
        service.audit_ledger = FailingLedger(audit.path)
    with pytest.raises((OSError, ReferenceDecisionAuditError)):
        service.withdraw(
            "one", notes="Reason", expected_revision=1,
            confirmed=True, request_id=f"fail-{failure_point}",
        )
    assert queue.path.read_bytes() == before_queue
    assert library.index_path.read_bytes() == before_index
    assert accepted_directory.is_dir()
    assert sorted(path.name for path in accepted_directory.iterdir()) == before_files
    assert queue.resolve_media_path(queue.get("one")["media_path"]).is_file()
    events = ReferenceDecisionAuditLedger(audit.path).history("one")
    if failure_point == "audit":
        assert [event["result"] for event in events] == ["success"]
    else:
        assert events[-1]["result"] == "failure"


def test_consistency_validation_detects_accepted_then_rejected_state(tmp_path):
    _, queue, _, _, service = environment(tmp_path)
    accept(service, queue)
    force_legacy_status(
        queue, "one", "rejected",
        notes="legacy inconsistent decision",
    )
    problems = service.consistency_problems()
    assert any("rejected candidate retains accepted_reference_id" in p for p in problems)
    assert any("strict index still lists" in p for p in problems)
    with pytest.raises(ReferenceDiscoveryError, match="withdrawal repair"):
        service.validate_consistency()


def test_consistency_validation_detects_missing_mismatched_and_profile_state(
    tmp_path,
):
    data_root, queue, library, _, service = environment(
        tmp_path, video_ids=("accepted", "duplicate", "withdrawn")
    )
    force_legacy_status(queue, "accepted", "accepted")
    queue.decide(
        "duplicate", "duplicate",
        accepted_reference_id="youtube-duplicate",
    )
    profile = data_root / "reference_profiles" / "gaming_highlight.json"
    profile.parent.mkdir(parents=True)
    profile.write_text(
        json.dumps(
            {
                "version": 1, "profile_name": "gaming_highlight",
                "reference_ids": ["youtube-withdrawn"],
            }
        ),
        encoding="utf-8",
    )
    problems = service.consistency_problems()
    assert any("accepted candidate has no accepted_reference_id" in p for p in problems)
    assert any("duplicate candidate retains" in p for p in problems)
    assert any("remains in profile gaming_highlight" in p for p in problems)
    assert library.list_references() == []


def test_cli_history_and_withdraw_use_same_service_layer(
    tmp_path, capsys, monkeypatch
):
    data_root, queue, library, audit, service = environment(tmp_path)
    accept(service, queue)
    force_legacy_status(
        queue, "one", "rejected",
        notes="legacy inconsistent decision",
    )
    monkeypatch.setenv("AUTOCLIP_REVIEWER_NAME", "CLI reviewer")
    common = [
        "--queue-path", str(queue.path),
        "--data-root", str(data_root),
        "--reference-root", str(service.reference_root),
        "--reference-index", str(library.index_path),
        "--profile-directory", str(service.profile_root),
        "--audit-path", str(audit.path),
    ]
    assert main(
        common + [
            "withdraw", "one", "--status", "rejected",
            "--note", "Does not match the desired clip style",
        ]
    ) == 0
    output = capsys.readouterr().out
    assert "Withdrew youtube-one" in output
    assert queue.get("one")["status"] == "rejected"
    assert main(common + ["history", "one"]) == 0
    history_output = capsys.readouterr().out
    assert '"action": "withdraw"' in history_output
    assert '"reviewer": "CLI reviewer"' in history_output


def test_cli_validate_returns_nonzero_for_legacy_inconsistency(
    tmp_path, capsys
):
    data_root, queue, library, audit, service = environment(tmp_path)
    accept(service, queue)
    force_legacy_status(
        queue, "one", "rejected", notes="legacy decision"
    )
    result = main(
        [
            "--queue-path", str(queue.path),
            "--data-root", str(data_root),
            "--reference-root", str(service.reference_root),
            "--reference-index", str(library.index_path),
            "--profile-directory", str(service.profile_root),
            "--audit-path", str(audit.path),
            "validate",
        ]
    )
    assert result == 1
    assert "withdrawal repair workflow" in capsys.readouterr().err


def test_audit_ledger_is_append_only_and_strict(tmp_path):
    ledger = ReferenceDecisionAuditLedger(tmp_path / "events.jsonl")
    first = ledger.event(
        video_id="one", action="reject", previous_status="discovered",
        requested_status="rejected", resulting_status="rejected",
        previous_revision=0, resulting_revision=1,
        accepted_reference_id_before=None,
        accepted_reference_id_after=None, result="success",
        note="Not suitable", request_id="request-one",
    )
    second = ledger.event(
        video_id="one", action="reconsider", previous_status="rejected",
        requested_status="discovered", resulting_status="discovered",
        previous_revision=1, resulting_revision=2,
        accepted_reference_id_before=None,
        accepted_reference_id_after=None, result="success",
        note="", request_id="request-two",
    )
    ledger.append(first)
    prefix = ledger.path.read_bytes()
    ledger.append(second)
    assert ledger.path.read_bytes().startswith(prefix)
    assert ledger.history("one") == [first, second]
    ledger.path.write_text("{bad json}\n", encoding="utf-8")
    with pytest.raises(ReferenceDecisionAuditError, match="corrupt"):
        ledger.history()
