from __future__ import annotations

import json
import os
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.app import path_migration as migration_cli
from backend.services.path_migration import (
    PathMigrationError,
    PathMigrationService,
    build_media_coverage,
)
from backend.services.clip_review_queue import ClipReviewQueue
from backend.services.persistent_paths import (
    PersistentPathError,
    PersistentPathResolver,
)
from backend.services.publication import sha256_file
from backend.services.video_manifest import (
    VideoManifest,
    VideoStatus,
    default_clip_analysis,
    default_transcription,
)


def _record(video_id: str, local_path: str | None) -> dict:
    return {
        "video_id": video_id,
        "source_platform": "youtube",
        "channel_name": "Creator",
        "channel_url": "https://www.youtube.com/@creator",
        "video_url": f"https://www.youtube.com/watch?v={video_id}",
        "title": "Title",
        "uploader": "Creator",
        "upload_date": "20260101",
        "duration_seconds": 60,
        "discovered_at": "2026-01-01T00:00:00+00:00",
        "downloaded_at": "2026-01-01T00:01:00+00:00",
        "local_file_path": local_path,
        "status": VideoStatus.DOWNLOADED.value,
        "error_message": None,
        "transcription": default_transcription(),
        "clip_analysis": default_clip_analysis(),
    }


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


@pytest.fixture
def migration_fixture(tmp_path):
    root = tmp_path / "persistent" / "data"
    root.mkdir(parents=True)
    legacy = tmp_path / "development" / "data"
    legacy.parent.mkdir(parents=True)
    legacy.symlink_to(root, target_is_directory=True)
    media = root / "downloads" / "Creator" / "video.mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"source-media")
    manifest_path = root / "manifests" / "videos.json"
    _write_json(
        manifest_path,
        {"version": 3, "videos": [_record("video", str(legacy / "downloads/Creator/video.mp4"))]},
    )
    service = PathMigrationService(
        root, legacy_roots=[legacy], migration_root=root / "path_migration"
    )
    return service, root, legacy, media, manifest_path


def test_classifies_canonical_legacy_release_url_and_immutable(migration_fixture):
    service, root, legacy, media, _ = migration_fixture
    audit = root / "reference_evidence_recovery" / "events.jsonl"
    audit.parent.mkdir(parents=True)
    audit.write_text(json.dumps({"media_path": str(legacy / "downloads/Creator/video.mp4")}) + "\n")

    inventory = service.inventory()
    classes = inventory["classification_counts"]

    assert classes["legacy_development_absolute"] == 1
    assert classes["external_url"] == 2
    assert classes["intentionally_immutable_historical_text"] == 1
    assert service.paths.classify("downloads/Creator/video.mp4").category == "canonical_persistent_relative"
    release = str(
        Path.home() / "clip-factory-production/releases"
        / ("a" * 40) / "data/video.mp4"
    )
    assert service.paths.classify(release).category == "production_release_absolute"


def test_plan_pins_hash_identity_checksum_owner_and_reason(migration_fixture):
    service, _, _, media, manifest_path = migration_fixture

    plan = service.plan()

    assert len(plan["changes"]) == 1
    change = plan["changes"][0]
    assert change["schema"] == "source_manifest"
    assert change["owning_record"] == "video_id=video"
    assert change["field_name"] == "local_file_path"
    assert change["proposed_value"] == "downloads/Creator/video.mp4"
    assert change["checksum_sha256"] == sha256_file(media)
    assert change["size_bytes"] == media.stat().st_size
    assert change["owner_uid"] == os.getuid()
    assert change["target_identity"] == {
        "device": media.stat().st_dev, "inode": media.stat().st_ino,
    }
    assert plan["source_documents"][0]["source_sha256"] == sha256_file(manifest_path)
    assert plan["content_sha256"]
    assert service.show(plan["plan_id"]) == plan


def test_plan_is_read_only_except_ignored_plan_and_sanitized_audit(migration_fixture):
    service, _, _, media, manifest_path = migration_fixture
    before = manifest_path.read_bytes()
    source_before = media.read_bytes()

    plan = service.plan()

    assert manifest_path.read_bytes() == before
    assert media.read_bytes() == source_before
    assert service.plan_path(plan["plan_id"]).is_file()
    event = json.loads(service.audit_path.read_text(encoding="utf-8").splitlines()[-1])
    assert event["event"] == "plan_created"
    assert "token" not in event and "cookie" not in event


def test_production_release_and_unrecognized_paths_require_manual_review(migration_fixture):
    service, root, _, _, manifest_path = migration_fixture
    value = json.loads(manifest_path.read_text())
    value["videos"][0]["local_file_path"] = str(
        Path.home() / "clip-factory-production/current/data/downloads/video.mp4"
    )
    value["videos"].append(_record("other", "/opt/unknown/video.mp4"))
    _write_json(manifest_path, value)

    plan = service.plan()

    assert plan["changes"] == []
    assert {item["classification"] for item in plan["manual_review"]} == {
        "production_release_absolute", "unsafe_or_unrecognized",
    }


def test_production_release_path_has_controlled_canonical_reader_mapping(tmp_path):
    root = tmp_path / "persistent/data"
    media = root / "downloads/video.mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"media")
    production = tmp_path / "clip-factory-production"
    release_root = production / "releases" / ("b" * 40)
    release_root.mkdir(parents=True)
    (release_root / "data").symlink_to(root, target_is_directory=True)
    (production / "current").symlink_to(release_root, target_is_directory=True)
    resolver = PersistentPathResolver(
        root, legacy_roots=[], production_root=production
    )
    release = (
        production / "releases" / ("b" * 40)
        / "data/downloads/video.mp4"
    )
    current = production / "current/data/downloads/video.mp4"

    assert resolver.materialize(str(release)) == media
    assert resolver.resolve(str(release), must_exist=True, regular=True) == media
    assert resolver.resolve(str(current), must_exist=True, regular=True) == media
    target, info = resolver.validate_migration_target(str(release))
    assert target == media and info.st_ino == media.stat().st_ino
    invalid = production / "releases/not-a-hash/data/downloads/video.mp4"
    assert resolver.classify(str(invalid)).category == "unsafe_or_unrecognized"


def test_production_release_path_is_planned_only_after_identity_validation(tmp_path):
    root = tmp_path / "persistent/data"
    media = root / "downloads/video.mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"media")
    production = tmp_path / "production"
    release = production / "releases" / ("c" * 40)
    release.mkdir(parents=True)
    (release / "data").symlink_to(root, target_is_directory=True)
    _write_json(root / "manifests/videos.json", {
        "version": 3,
        "videos": [_record("video", str(release / "data/downloads/video.mp4"))],
    })
    service = PathMigrationService(
        root, legacy_roots=[], production_root=production
    )

    plan = service.plan()

    assert len(plan["changes"]) == 1
    assert plan["changes"][0]["proposed_value"] == "downloads/video.mp4"
    assert plan["manual_review"] == []


def test_target_identity_mismatch_refused(tmp_path):
    root = tmp_path / "persistent"
    legacy = tmp_path / "legacy"
    for directory, contents in ((root, b"canonical"), (legacy, b"different")):
        (directory / "downloads").mkdir(parents=True)
        (directory / "downloads/video.mp4").write_bytes(contents)
    resolver = PersistentPathResolver(root, legacy_roots=[legacy])

    with pytest.raises(PersistentPathError, match="same file"):
        resolver.validate_migration_target(str(legacy / "downloads/video.mp4"))


def test_checksum_mismatch_refused(migration_fixture):
    service, _, legacy, _, _ = migration_fixture
    with pytest.raises(PersistentPathError, match="checksum"):
        service.paths.validate_migration_target(
            str(legacy / "downloads/Creator/video.mp4"), checksum="0" * 64
        )


def test_symlink_special_traversal_and_owner_are_refused(migration_fixture):
    service, root, legacy, media, manifest_path = migration_fixture
    link = root / "downloads" / "Creator" / "link.mp4"
    link.symlink_to(media)
    value = json.loads(manifest_path.read_text())
    value["videos"][0]["local_file_path"] = str(legacy / "downloads/Creator/link.mp4")
    _write_json(manifest_path, value)
    plan = service.plan()
    assert len(plan["manual_review"]) == 1
    assert "symbolic link" in plan["manual_review"][0]["manual_reason"]
    with pytest.raises(PersistentPathError, match="traversal"):
        service.paths.resolve("downloads/../outside.mp4")
    value["videos"][0]["local_file_path"] = str(
        legacy / "downloads/Creator/video.mp4"
    )
    _write_json(manifest_path, value)
    unexpected = PathMigrationService(
        root, legacy_roots=[legacy], owner_uid=os.getuid() + 1
    ).plan()
    assert not unexpected["changes"]
    assert "ownership" in unexpected["manual_review"][0]["manual_reason"]


def test_overlapping_legacy_roots_are_ambiguous(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    resolver = PersistentPathResolver(
        root, legacy_roots=[tmp_path / "legacy", tmp_path / "legacy/data"]
    )
    classification = resolver.classify(str(tmp_path / "legacy/data/file.mp4"))
    assert classification.category == "unsafe_or_unrecognized"
    assert "multiple historical roots" in classification.reason


def test_apply_atomically_normalizes_and_never_moves_media(migration_fixture):
    service, _, _, media, manifest_path = migration_fixture
    plan = service.plan()
    inode = media.stat().st_ino
    contents = media.read_bytes()

    recovery = service.apply(plan["plan_id"], confirm=plan["plan_id"])

    stored = json.loads(manifest_path.read_text())["videos"][0]["local_file_path"]
    assert stored == "downloads/Creator/video.mp4"
    assert recovery["state"] == "completed"
    assert recovery["documents"][0]["status"] == "migrated"
    assert media.stat().st_ino == inode and media.read_bytes() == contents
    assert not (service.data_root / "quarantine").exists()


def test_apply_requires_exact_confirmation_and_is_idempotent(migration_fixture):
    service, _, _, _, _ = migration_fixture
    plan = service.plan()
    with pytest.raises(PathMigrationError, match="confirmation"):
        service.apply(plan["plan_id"], confirm="wrong")
    first = service.apply(plan["plan_id"], confirm=plan["plan_id"])
    second = service.apply(plan["plan_id"], confirm=plan["plan_id"])
    assert second == first


def test_changed_source_record_is_skipped_independently(migration_fixture):
    service, root, legacy, _, manifest_path = migration_fixture
    second_media = root / "downloads/Creator/second.mp4"
    second_media.write_bytes(b"second")
    second = root / "transcripts/second/transcript.json"
    _write_json(second, {
        "version": 1, "video_id": "second",
        "source_media_path": str(legacy / "downloads/Creator/second.mp4"),
        "segments": [],
    })
    plan = service.plan()
    original = json.loads(manifest_path.read_text())
    original["videos"][0]["title"] = "Changed after planning"
    _write_json(manifest_path, original)

    recovery = service.apply(plan["plan_id"], confirm=plan["plan_id"])
    statuses = {item["document_path"]: item["status"] for item in recovery["documents"]}
    assert statuses["manifests/videos.json"] == "skipped"
    assert statuses["transcripts/second/transcript.json"] == "migrated"


def test_recovery_restores_original_record(migration_fixture):
    service, _, legacy, _, manifest_path = migration_fixture
    before = manifest_path.read_bytes()
    plan = service.plan()
    recovery = service.apply(plan["plan_id"], confirm=plan["plan_id"])

    restored = service.restore(recovery["recovery_id"])

    assert restored["state"] == "restored"
    assert manifest_path.read_bytes() == before
    assert json.loads(manifest_path.read_text())["videos"][0]["local_file_path"].startswith(str(legacy))
    assert service.restore(recovery["recovery_id"])["state"] == "restored"


def test_restore_refuses_changed_post_migration_record(migration_fixture):
    service, _, _, _, manifest_path = migration_fixture
    plan = service.plan()
    recovery = service.apply(plan["plan_id"], confirm=plan["plan_id"])
    value = json.loads(manifest_path.read_text())
    value["videos"][0]["title"] = "operator change"
    _write_json(manifest_path, value)
    with pytest.raises(PathMigrationError, match="changed record"):
        service.restore(recovery["recovery_id"])


def test_atomic_replace_failure_leaves_source_and_recovery_is_actionable(migration_fixture):
    service, _, _, _, manifest_path = migration_fixture
    before = manifest_path.read_bytes()
    plan = service.plan()
    real_replace = os.replace

    def fail_record(source, target):
        if Path(target) == manifest_path:
            raise OSError("interrupted replacement")
        return real_replace(source, target)

    with patch("backend.services.path_migration.os.replace", side_effect=fail_record):
        with pytest.raises(OSError, match="interrupted"):
            service.apply(plan["plan_id"], confirm=plan["plan_id"])
    assert manifest_path.read_bytes() == before
    recovery_manifests = list(service.recovery_root.glob("*/manifest.json"))
    assert len(recovery_manifests) == 1
    interrupted = json.loads(recovery_manifests[0].read_text())
    assert interrupted["state"] == "applying"
    assert interrupted["documents"][0]["status"] == "prepared"
    restored = service.restore(interrupted["recovery_id"])
    assert restored["state"] == "restored"
    assert manifest_path.read_bytes() == before


def test_immutable_historical_audit_is_never_rewritten(migration_fixture):
    service, root, legacy, _, _ = migration_fixture
    audit = root / "reference_evidence_recovery/events.jsonl"
    audit.parent.mkdir(parents=True)
    original = json.dumps({"media_path": str(legacy / "downloads/Creator/video.mp4")}) + "\n"
    audit.write_text(original, encoding="utf-8")
    plan = service.plan()
    service.apply(plan["plan_id"], confirm=plan["plan_id"])
    assert audit.read_text(encoding="utf-8") == original


def test_mixed_legacy_and_canonical_manifest_reads_and_new_writes(migration_fixture):
    _, root, legacy, media, manifest_path = migration_fixture
    other = root / "downloads/Creator/other.mp4"
    other.write_bytes(b"other")
    value = json.loads(manifest_path.read_text())
    value["videos"].append(_record("other", "downloads/Creator/other.mp4"))
    _write_json(manifest_path, value)
    manifest = VideoManifest(manifest_path, data_root=root,
                             path_resolver=PersistentPathResolver(root, legacy_roots=[legacy]))

    assert manifest.get("video")["local_file_path"] == str(media)
    assert manifest.get("other")["local_file_path"] == str(other)
    third = root / "downloads/Creator/third.mp4"
    third.write_bytes(b"third")
    manifest.upsert(_record("third", str(third)))
    stored = json.loads(manifest_path.read_text())["videos"][-1]["local_file_path"]
    assert stored == "downloads/Creator/third.mp4"


def test_routine_manifest_update_preserves_unchanged_legacy_field(migration_fixture):
    _, root, legacy, _, manifest_path = migration_fixture
    manifest = VideoManifest(
        manifest_path, data_root=root,
        path_resolver=PersistentPathResolver(root, legacy_roots=[legacy]),
    )
    current = manifest.get("video")
    current["title"] = "Metadata refresh"

    manifest.upsert(current)

    raw = json.loads(manifest_path.read_text())["videos"][0]
    assert raw["title"] == "Metadata refresh"
    assert raw["local_file_path"].startswith(str(legacy))


def test_routine_review_update_preserves_unchanged_legacy_paths(tmp_path):
    root = tmp_path / "persistent/data"
    legacy = tmp_path / "development/data"
    root.mkdir(parents=True)
    legacy.parent.mkdir(parents=True)
    legacy.symlink_to(root, target_is_directory=True)
    preview = root / "previews/video/candidate.mp4"
    metadata = preview.with_suffix(".json")
    preview.parent.mkdir(parents=True)
    preview.write_bytes(b"preview")
    metadata.write_text("{}")
    resolver = PersistentPathResolver(root, legacy_roots=[legacy])
    queue = ClipReviewQueue(
        root / "review_queue/reviews.json", data_root=root,
        path_resolver=resolver,
    )
    candidate = {
        "candidate_id": "candidate", "rank": 1, "score": 90,
        "start": 1.0, "end": 11.0, "duration": 10.0, "text": "beat",
    }
    queue.add_or_update_preview("video", candidate, preview, metadata)
    document = json.loads(queue.path.read_text())
    document["items"][0]["preview_path"] = str(
        legacy / "previews/video/candidate.mp4"
    )
    document["items"][0]["preview_metadata_path"] = str(
        legacy / "previews/video/candidate.json"
    )
    _write_json(queue.path, document)

    queue.add_or_update_preview("video", candidate, preview, metadata)

    raw = json.loads(queue.path.read_text())["items"][0]
    assert raw["preview_path"].startswith(str(legacy))
    assert raw["preview_metadata_path"].startswith(str(legacy))


def test_orphan_association_requires_artifact_lineage_and_manifest_identity(tmp_path):
    root = tmp_path / "data"
    orphan = root / "downloads/Creator/orphan.mp4"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"orphan")
    _write_json(root / "manifests/videos.json", {
        "version": 3, "videos": [_record("orphan-video", None)]
    })
    _write_json(root / "transcripts/orphan-video/transcript.json", {
        "version": 1, "video_id": "orphan-video",
        "source_media_path": "downloads/Creator/orphan.mp4", "segments": [],
    })
    service = PathMigrationService(root, legacy_roots=[])

    plan = service.plan()
    association = plan["orphan_analysis"]["proven_associations"][0]

    assert association["video_id"] == "orphan-video"
    assert association["creator"] == "Creator"
    assert association["checksum_sha256"] == sha256_file(orphan)
    assert association["evidence_paths"] == ["transcripts/orphan-video/transcript.json"]
    assert association["dependencies"]["transcripts"] == [
        "transcripts/orphan-video/transcript.json"
    ]
    recovery = service.apply(plan["plan_id"], confirm=plan["plan_id"])
    registry = json.loads(service.registry_path.read_text())
    assert registry["entries"][0]["video_id"] == "orphan-video"
    assert recovery["orphan_registry"]["status"] == "completed"
    service.restore(recovery["recovery_id"])
    assert not service.registry_path.exists()


def test_interrupted_orphan_registry_write_is_recoverable(tmp_path):
    root = tmp_path / "data"
    orphan = root / "downloads/orphan.mp4"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"orphan")
    _write_json(root / "manifests/videos.json", {
        "version": 3, "videos": [_record("video", None)]
    })
    _write_json(root / "transcripts/video/transcript.json", {
        "version": 1, "video_id": "video",
        "source_media_path": "downloads/orphan.mp4", "segments": [],
    })
    service = PathMigrationService(root, legacy_roots=[])
    plan = service.plan()
    real_replace = os.replace

    def fail_registry(source, target):
        if Path(target) == service.registry_path:
            raise OSError("interrupted registry replacement")
        return real_replace(source, target)

    with patch("backend.services.path_migration.os.replace", side_effect=fail_registry):
        with pytest.raises(OSError, match="interrupted registry"):
            service.apply(plan["plan_id"], confirm=plan["plan_id"])
    recovery_path = next(service.recovery_root.glob("*/manifest.json"))
    interrupted = json.loads(recovery_path.read_text())
    assert interrupted["orphan_registry"]["status"] == "prepared"
    restored = service.restore(interrupted["recovery_id"])
    assert restored["state"] == "restored"
    assert not service.registry_path.exists()


def test_orphan_association_is_revalidated_immediately_before_apply(tmp_path):
    root = tmp_path / "data"
    orphan = root / "downloads/orphan.mp4"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"orphan")
    _write_json(root / "manifests/videos.json", {
        "version": 3, "videos": [_record("video", None)]
    })
    evidence = root / "transcripts/video/transcript.json"
    _write_json(evidence, {
        "version": 1, "video_id": "video",
        "source_media_path": "downloads/orphan.mp4", "segments": [],
    })
    service = PathMigrationService(root, legacy_roots=[])
    plan = service.plan()
    value = json.loads(evidence.read_text())
    value["video_id"] = "changed"
    _write_json(evidence, value)

    recovery = service.apply(plan["plan_id"], confirm=plan["plan_id"])

    assert recovery["orphan_registry"]["added"] == 0
    assert recovery["orphan_registry"]["skipped"][0]["video_id"] == "video"
    assert not service.registry_path.exists()


def test_conflicting_orphan_ownership_is_never_associated(tmp_path):
    root = tmp_path / "data"
    orphan = root / "downloads/orphan.mp4"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"orphan")
    _write_json(root / "manifests/videos.json", {
        "version": 3, "videos": [_record("one", None), _record("two", None)]
    })
    for folder, video_id in (("transcripts", "one"), ("clip_candidates", "two")):
        filename = "transcript.json" if folder == "transcripts" else "candidates.json"
        _write_json(root / folder / video_id / filename, {
            "version": 1, "video_id": video_id,
            "source_media_path": "downloads/orphan.mp4",
        })
    analysis = PathMigrationService(root, legacy_roots=[]).plan()["orphan_analysis"]
    assert analysis["proven_associations"] == []
    assert analysis["conflicting_ownership"][0]["conflicting_video_ids"] == ["one", "two"]


def test_unverified_orphan_is_retained_and_manual_adoption_needs_proof(tmp_path):
    root = tmp_path / "data"
    orphan = root / "downloads/orphan.mp4"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"orphan")
    service = PathMigrationService(root, legacy_roots=[])
    analysis = service.plan()["orphan_analysis"]
    assert analysis["orphaned_unverified"][0]["media_path"] == "downloads/orphan.mp4"
    assert orphan.exists()
    evidence = root / "transcripts/wrong/transcript.json"
    _write_json(evidence, {"video_id": "wrong", "source_media_path": "downloads/orphan.mp4"})
    with pytest.raises(PathMigrationError, match="prove exactly one"):
        service.adopt_orphan(
            media_path="downloads/orphan.mp4", video_id="requested", creator="Creator",
            checksum_sha256=sha256_file(orphan), evidence_paths=["transcripts/wrong/transcript.json"],
            confirm=sha256_file(orphan),
        )


def test_manual_orphan_adoption_is_checksum_confirmed_and_conflict_safe(tmp_path):
    root = tmp_path / "data"
    orphan = root / "downloads/orphan.mp4"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"orphan")
    evidence = root / "transcripts/video/transcript.json"
    _write_json(evidence, {"video_id": "video", "source_media_path": "downloads/orphan.mp4"})
    service = PathMigrationService(root, legacy_roots=[])
    checksum = sha256_file(orphan)
    adopted = service.adopt_orphan(
        media_path="downloads/orphan.mp4", video_id="video", creator="Creator",
        checksum_sha256=checksum, evidence_paths=["transcripts/video/transcript.json"],
        confirm=checksum,
    )
    assert adopted["video_id"] == "video"
    with pytest.raises(PathMigrationError, match="confirmation"):
        service.adopt_orphan(
            media_path="downloads/orphan.mp4", video_id="other", creator="Creator",
            checksum_sha256=checksum, evidence_paths=["transcripts/video/transcript.json"],
            confirm="wrong",
        )


def test_corrupt_orphan_registry_fails_closed(tmp_path):
    root = tmp_path / "data"
    _write_json(root / "path_migration/orphan_ownership.json", {
        "version": 1, "updated_at": "now",
        "entries": [{"media_path": "../escape.mp4"}],
    })
    with pytest.raises(PathMigrationError, match="entry is malformed"):
        build_media_coverage(root)


def test_media_coverage_counts_each_inode_once_and_all_categories(tmp_path):
    root = tmp_path / "data"
    managed = root / "downloads/managed.mp4"
    orphan = root / "downloads/orphan.mp4"
    reference = root / "reference_clips/ref/reference.mp4"
    recovery = root / "reference_evidence_recovery/ref/reference.mp4"
    discovery = root / "reference_discovery/media/candidate.mp4"
    for path, contents in (
        (managed, b"managed"), (orphan, b"orphan"), (reference, b"reference"),
        (recovery, b"recovery"), (discovery, b"discovery"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
    hardlink = managed.with_name("managed-copy.mp4")
    os.link(managed, hardlink)
    _write_json(root / "manifests/videos.json", {
        "version": 3, "videos": [_record("managed", "downloads/managed.mp4")]
    })
    _write_json(root / "reference_clips/index.json", {
        "version": 1, "updated_at": "2026-01-01T00:00:00+00:00",
        "references": [{"reference_id": "ref", "media_path": "reference_clips/ref/reference.mp4"}],
    })

    coverage = build_media_coverage(root)

    assert coverage["file_count"] == 5
    assert coverage["bytes"] == sum(map(len, (b"managed", b"orphan", b"reference", b"recovery", b"discovery")))
    assert coverage["duplicate_identity_count"] == 1
    assert coverage["categories"]["managed_mapped"]["file_count"] == 1
    assert coverage["categories"]["orphaned_unverified"]["file_count"] == 1
    assert coverage["categories"]["reference_protected"]["file_count"] == 1
    assert coverage["categories"]["recovery_protected"]["file_count"] == 1
    assert coverage["categories"]["unmanaged_ignored"]["file_count"] == 1
    assert coverage["eligible_bytes"] == 0


def test_normalization_alone_does_not_make_cleanup_eligible(migration_fixture):
    service, root, _, media, _ = migration_fixture
    before = build_media_coverage(root)
    plan = service.plan()
    service.apply(plan["plan_id"], confirm=plan["plan_id"])
    after = build_media_coverage(root)
    assert before["eligible_bytes"] == after["eligible_bytes"] == 0
    assert media.exists()


def test_cli_explicit_argv_plan_show_apply_restore(migration_fixture):
    service, _, _, _, _ = migration_fixture
    output = StringIO()
    assert migration_cli.main(["plan"], service=service, stdout=output) == 0
    plan_id = output.getvalue().splitlines()[0].split(": ", 1)[1]
    assert migration_cli.main(
        ["show", "--plan-id", plan_id], service=service,
        stdout=StringIO(), stderr=StringIO(),
    ) == 0
    applied = StringIO()
    assert migration_cli.main(
        ["apply", "--plan-id", plan_id, "--confirm", plan_id],
        service=service, stdout=applied, stderr=StringIO(),
    ) == 0
    recovery_id = json.loads(applied.getvalue())["recovery_id"]
    assert migration_cli.main(
        ["restore", "--recovery-id", recovery_id], service=service,
        stdout=StringIO(), stderr=StringIO(),
    ) == 0


def test_cli_failure_is_nonzero_and_never_publishes(migration_fixture):
    service, root, _, media, _ = migration_fixture
    before = media.read_bytes()
    error = StringIO()
    assert migration_cli.main(
        ["apply", "--plan-id", "invalid", "--confirm", "invalid"],
        service=service, stdout=StringIO(), stderr=error,
    ) == 1
    assert "failed safely" in error.getvalue()
    assert media.read_bytes() == before
    assert not (root / "publication").exists()
