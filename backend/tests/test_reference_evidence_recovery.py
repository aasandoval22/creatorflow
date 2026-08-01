from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from backend.services.reference_evidence_recovery import (
    ReferenceEvidenceRecoveryError, ReferenceEvidenceRecoveryService,
)
from backend.services.reference_evidence_service import _atomic_restore
from backend.app import reference_clips
from backend.tests.test_reference_evidence import annotation_values, environment


def protected_snapshot(tmp_path: Path, library, builder, reference_id: str) -> Path:
    snapshot = tmp_path / "protected-snapshot"
    snapshot.mkdir(mode=0o700)
    (snapshot / "reference_profiles").mkdir()
    shutil.copyfile(library.index_path, snapshot / "index.json")
    profile = builder.profile_path("gaming_highlight")
    shutil.copyfile(profile, snapshot / "reference_profiles" / profile.name)
    (snapshot / "reference_annotations.absent").touch(mode=0o600)
    digest = hashlib.sha256(profile.read_bytes()).hexdigest()
    (snapshot / "checksums.sha256").write_text(
        f"{digest}  data/reference_profiles/gaming_highlight.json\n",
        encoding="utf-8",
    )
    return snapshot


def prepared(tmp_path: Path):
    library, entries, annotations, audit, builder, evidence = environment(tmp_path, count=1)
    reference_id = entries[0]["reference_id"]
    original = builder.build("gaming_highlight")
    original_bytes = builder.profile_path("gaming_highlight").read_bytes()
    snapshot = protected_snapshot(tmp_path, library, builder, reference_id)
    evidence.update_annotations(
        reference_id, expected_revision=0,
        values=annotation_values(pacing="fast"), request_id="mutate-annotation",
    )
    evidence.rebuild_profile("gaming_highlight", request_id="mutate-profile")
    return (library, entries[0], annotations, audit, builder, evidence,
            reference_id, snapshot, original, original_bytes)


def test_valid_recovery_restores_absence_and_profile_bytes_with_backup(tmp_path):
    _, entry, annotations, audit, builder, evidence, reference_id, snapshot, _, original = prepared(tmp_path)
    unrelated_annotation = annotations.path("youtube-unrelated")
    unrelated_annotation.parent.mkdir(parents=True, exist_ok=True)
    unrelated_annotation.write_text(
        json.dumps(annotations.default("youtube-unrelated")), encoding="utf-8"
    )
    unrelated_profile = builder.output_directory / "personality_reaction.json"
    unrelated_profile.write_bytes(b'{"unrelated":"profile"}\n')
    unrelated_before = (unrelated_annotation.read_bytes(), unrelated_profile.read_bytes())
    analysis_before = Path(entry["analysis_path"]).read_bytes()
    index_before = evidence.library.index_path.read_bytes()
    current_profile = builder.profile_path("gaming_highlight").read_bytes()
    current_annotation = annotations.path(reference_id).read_bytes()
    recovery = ReferenceEvidenceRecoveryService(
        evidence, recovery_root=tmp_path / "recoveries"
    )
    result = recovery.restore(
        reference_id, "gaming_highlight", snapshot,
        reason="Undo unintended evidence token=do-not-record", request_id="restore-one",
    )
    assert result["status"] == "restored"
    assert not annotations.path(reference_id).exists()
    assert builder.profile_path("gaming_highlight").read_bytes() == original
    backup = Path(result["recovery_path"])
    assert (backup / "profile.before.json").read_bytes() == current_profile
    assert (backup / "annotation.before.json").read_bytes() == current_annotation
    assert (backup / "annotation.displaced.json").read_bytes() == current_annotation
    assert Path(entry["analysis_path"]).read_bytes() == analysis_before
    assert evidence.library.index_path.read_bytes() == index_before
    assert (unrelated_annotation.read_bytes(), unrelated_profile.read_bytes()) == unrelated_before
    event = audit.history(reference_id=reference_id)[-1]
    assert event["action"] == "evidence_recovery" and event["result"] == "success"
    assert event["resulting_annotation_state"] == "absent"
    assert "do-not-record" not in json.dumps(event)
    assert event["new_profile_sha256"] == hashlib.sha256(original).hexdigest()
    again = recovery.restore(
        reference_id, "gaming_highlight", snapshot,
        reason="Safe retry", request_id="restore-two",
    )
    assert again["status"] == "already_restored" and again["recovery_path"] is None


@pytest.mark.parametrize("failure_target", ["annotation", "profile"])
def test_recovery_rolls_back_after_write_failure(tmp_path, failure_target):
    _, _, annotations, audit, builder, evidence, reference_id, snapshot, _, _ = prepared(tmp_path)
    if failure_target == "annotation":
        (snapshot / "reference_annotations.absent").unlink()
        target_root = snapshot / "reference_annotations"
        target_root.mkdir(mode=0o700)
        (target_root / f"{reference_id}.json").write_text(
            json.dumps(annotations.default(reference_id)), encoding="utf-8"
        )
    annotation_before = annotations.path(reference_id).read_bytes()
    profile_before = builder.profile_path("gaming_highlight").read_bytes()
    calls = 0

    def writer(path: Path, value: bytes | None) -> None:
        nonlocal calls
        calls += 1
        if (failure_target == "annotation" and path == annotations.path(reference_id)) or (
            failure_target == "profile" and path == builder.profile_path("gaming_highlight")
        ):
            raise OSError(f"synthetic {failure_target} failure")
        _atomic_restore(path, value)

    service = ReferenceEvidenceRecoveryService(
        evidence, recovery_root=tmp_path / "recoveries", state_writer=writer,
    )
    with pytest.raises(ReferenceEvidenceRecoveryError, match="current state was restored"):
        service.restore(
            reference_id, "gaming_highlight", snapshot,
            reason="Rollback fixture", request_id=f"failure-{failure_target}",
        )
    assert annotations.path(reference_id).read_bytes() == annotation_before
    assert builder.profile_path("gaming_highlight").read_bytes() == profile_before
    assert audit.history(reference_id=reference_id)[-1]["result"] == "failure"


def test_recovery_refuses_permissions_checksums_and_index_mismatch(tmp_path):
    _, _, annotations, audit, builder, evidence, reference_id, snapshot, _, _ = prepared(tmp_path)
    before = (annotations.path(reference_id).read_bytes(),
              builder.profile_path("gaming_highlight").read_bytes())
    service = ReferenceEvidenceRecoveryService(evidence, recovery_root=tmp_path / "recoveries")
    os.chmod(snapshot, 0o755)
    with pytest.raises(ReferenceEvidenceRecoveryError, match="inaccessible"):
        service.restore(reference_id, "gaming_highlight", snapshot, reason="bad permissions")
    os.chmod(snapshot, 0o700)
    (snapshot / "checksums.sha256").write_text(
        f"{'0' * 64}  data/reference_profiles/gaming_highlight.json\n", encoding="utf-8"
    )
    with pytest.raises(ReferenceEvidenceRecoveryError, match="checksum"):
        service.restore(reference_id, "gaming_highlight", snapshot, reason="bad checksum")
    assert (annotations.path(reference_id).read_bytes(),
            builder.profile_path("gaming_highlight").read_bytes()) == before
    # Restore checksum, then change one strict-index field.
    digest = hashlib.sha256((snapshot / "reference_profiles" / "gaming_highlight.json").read_bytes()).hexdigest()
    (snapshot / "checksums.sha256").write_text(
        f"{digest}  data/reference_profiles/gaming_highlight.json\n", encoding="utf-8"
    )
    index = json.loads((snapshot / "index.json").read_text(encoding="utf-8"))
    index["references"][0]["creator"] = "Different"
    (snapshot / "index.json").write_text(json.dumps(index), encoding="utf-8")
    with pytest.raises(ReferenceEvidenceRecoveryError, match="strict-index"):
        service.restore(reference_id, "gaming_highlight", snapshot, reason="bad index")


def test_recovery_cli_accepts_explicit_argv(tmp_path, capsys):
    library, _, annotations, audit, builder, _, reference_id, snapshot, _, original = prepared(tmp_path)
    assert reference_clips.main([
        "--reference-root", str(library.root),
        "--index-path", str(library.index_path),
        "--profile-directory", str(builder.output_directory),
        "--annotation-directory", str(annotations.root),
        "--evidence-audit-path", str(audit.path),
        "--recovery-directory", str(tmp_path / "recoveries"),
        "restore-evidence", "--reference-id", reference_id,
        "--profile", "gaming_highlight", "--snapshot", str(snapshot),
        "--reason", "Fixture recovery",
    ]) == 0
    assert "Recovery status: restored" in capsys.readouterr().out
    assert builder.profile_path("gaming_highlight").read_bytes() == original
