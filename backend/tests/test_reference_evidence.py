from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app import reference_clips
from backend.services.reference_annotations import (
    ANNOTATION_FIELDS,
    ReferenceAnnotationError,
    ReferenceAnnotationStore,
    default_annotation_values,
)
from backend.services.reference_clip_analyzer import (
    ReferenceAnalysisError,
    ReferenceClipAnalyzer,
)
from backend.services.reference_clip_library import ReferenceClipLibrary
from backend.services.reference_evidence_audit import (
    ReferenceEvidenceAuditError,
    ReferenceEvidenceAuditLedger,
)
from backend.services.reference_evidence_service import ReferenceEvidenceService
from backend.services.reference_profile_builder import ReferenceProfileBuilder


class Runner:
    def __call__(self, command):
        if "-show_streams" in command:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "streams": [
                        {
                            "codec_type": "video", "codec_name": "av1",
                            "width": 1080, "height": 1920,
                            "r_frame_rate": "60/1", "avg_frame_rate": "60/1",
                            "display_aspect_ratio": "9:16",
                        },
                        {
                            "codec_type": "audio", "codec_name": "opus",
                            "sample_rate": "48000",
                        },
                    ],
                    "format": {"duration": "45"},
                }),
                stderr="",
            )
        if "select=" in " ".join(command):
            return SimpleNamespace(
                returncode=0, stdout="", stderr="pts_time:2\npts_time:20"
            )
        return SimpleNamespace(
            returncode=0, stdout="",
            stderr="silence_start: 10\nsilence_end: 11 | silence_duration: 1",
        )


class Transcriber:
    def transcribe(self, *_args, **_kwargs):
        words = [
            SimpleNamespace(word="What?", start=0.4, end=0.8),
            SimpleNamespace(word="No", start=1.8, end=2.0),
            SimpleNamespace(word="way!", start=2.1, end=2.5),
            SimpleNamespace(word="because", start=30.0, end=30.4),
            SimpleNamespace(word="finally!", start=40.0, end=40.6),
        ]
        return iter([SimpleNamespace(words=words)]), SimpleNamespace(language="en")


class FailingTranscriber:
    def transcribe(self, *_args, **_kwargs):
        raise RuntimeError(
            "synthetic transcription failure token=must-not-leak trailing-private-value"
        )


class FailingAuditLedger(ReferenceEvidenceAuditLedger):
    def append(self, event):
        raise ReferenceEvidenceAuditError("synthetic durable append failure")


def baseline(reference_id: str, *, status: str = "accepted") -> dict:
    return {
        "version": 1,
        "reference_id": reference_id,
        "source_video_id": reference_id.removeprefix("youtube-"),
        "source_title": f"Title {reference_id}",
        "creator": "Creator",
        "status": status,
        "purpose": "creatorflow_baseline",
        "profile_name": "gaming_highlight",
        "qualities": ["human-authored quality"],
        "layout": {
            "orientation": "vertical", "composition": "unknown",
            "top_region": "unknown", "bottom_region": "unknown",
            "facecam_prominence": "unknown",
        },
        "story_structure": {
            "opening_style": "unknown", "setup_requirement": "unknown",
            "primary_focus": "unknown", "payoff_type": "unknown",
            "payoff_required": False, "ending_style": "unknown",
        },
        "timing_preferences": {
            "requires_long_lead_in": False,
            "requires_complete_setup": False,
            "requires_complete_payoff": False,
            "preferred_ending": "human review required",
        },
        "notes": "human-authored note",
    }


def environment(tmp_path: Path, *, count: int = 3):
    reference_root = tmp_path / "reference_clips"
    library = ReferenceClipLibrary(reference_root)
    entries = []
    for index in range(count):
        reference_id = f"youtube-ref-{index}"
        directory = reference_root / f"ref-{index}"
        directory.mkdir(parents=True)
        (directory / "reference.mp4").write_bytes(f"media-{index}".encode())
        (directory / "baseline.json").write_text(
            json.dumps(baseline(reference_id)), encoding="utf-8"
        )
        (directory / "reference.info.json").write_text("{}", encoding="utf-8")
        entry = library.register_directory(
            directory, profile_name="gaming_highlight"
        )
        ReferenceClipAnalyzer(library, runner=Runner()).analyze(
            reference_id, transcription=False
        )
        entries.append(entry)
    annotations = ReferenceAnnotationStore(tmp_path / "reference_annotations")
    audit = ReferenceEvidenceAuditLedger(
        tmp_path / "reference_annotations" / "events.jsonl"
    )
    builder = ReferenceProfileBuilder(
        library, tmp_path / "reference_profiles", annotation_store=annotations
    )
    service = ReferenceEvidenceService(
        library,
        annotations=annotations,
        audit=audit,
        profile_builder=builder,
        analyzer_factory=lambda value: ReferenceClipAnalyzer(
            value, runner=Runner(), transcriber=Transcriber()
        ),
        lock_path=tmp_path / "reference_annotations" / ".lock",
        reviewer_name="Fixture reviewer",
    )
    return library, entries, annotations, audit, builder, service


def annotation_values(**changes):
    values = default_annotation_values()
    values.update(changes)
    return values


def test_transcription_enabled_reanalysis_enriches_heuristic_schema(tmp_path):
    library, entries, annotations, audit, _, service = environment(tmp_path)
    reference_id = entries[0]["reference_id"]
    baseline_before = Path(entries[0]["baseline_path"]).read_bytes()
    annotation_before = annotations.read(reference_id)
    result = service.reanalyze(
        reference_id, transcription=True, force=True, request_id="reanalyze-one"
    )
    speech = result["speech"]
    assert result["version"] == 2 and result["analysis_revision"] == 2
    assert result["transcription"] == {
        "requested": True, "status": "available", "language": "en",
        "word_timestamps": True, "evidence_kind": "transcript_heuristic",
    }
    assert speech["word_count"] == 5 and speech["spoken_duration"] == 40.2
    assert speech["speech_density"] > 0 and speech["words_per_spoken_second"]
    assert speech["meaningful_pauses"] and speech["questions"]
    assert speech["reaction_signals"] and speech["payoff_signals"]
    assert speech["likely_hook"]["evidence_kind"] == "transcript_heuristic"
    assert speech["likely_payoff"]["timestamp"] == 40.6
    assert speech["post_payoff_tail"] == 4.4
    assert "quality" in result["limitations"][1]
    assert Path(entries[0]["baseline_path"]).read_bytes() == baseline_before
    assert annotations.read(reference_id) == annotation_before
    assert library.validate_checksum(reference_id)
    event = audit.history(reference_id=reference_id)[0]
    assert event["action"] == "reanalyze" and event["result"] == "success"


def test_failed_transcription_preserves_previous_analysis_atomically(tmp_path):
    library, entries, annotations, audit, builder, _ = environment(tmp_path)
    reference_id = entries[0]["reference_id"]
    path = Path(entries[0]["analysis_path"])
    before = path.read_bytes()
    service = ReferenceEvidenceService(
        library,
        annotations=annotations,
        audit=audit,
        profile_builder=builder,
        analyzer_factory=lambda value: ReferenceClipAnalyzer(
            value, runner=Runner(), transcriber=FailingTranscriber()
        ),
        lock_path=tmp_path / "reference_annotations" / ".lock",
    )
    with pytest.raises(ReferenceAnalysisError, match="synthetic transcription"):
        service.reanalyze(
            reference_id, transcription=True, force=True,
            request_id="failed-reanalysis",
        )
    assert path.read_bytes() == before
    failure = audit.history(reference_id=reference_id)[-1]
    assert failure["result"] == "failure"
    assert "synthetic transcription failure" in failure["failure_reason"]
    assert "must-not-leak" not in failure["failure_reason"]
    assert "trailing-private-value" not in failure["failure_reason"]
    assert "[redacted]" in failure["failure_reason"]
    assert not list(path.parent.glob(".analysis.json.*.tmp"))


def test_unknown_annotations_are_explicit_but_not_created(tmp_path):
    _, entries, annotations, audit, _, _ = environment(tmp_path)
    reference_id = entries[0]["reference_id"]
    document = annotations.read(reference_id)
    assert document["revision"] == 0 and document["updated_at"] is None
    assert set(document["annotations"]) == set(ANNOTATION_FIELDS)
    assert all(document["annotations"][name] == "unknown" for name in (
        "composition", "facecam_presence", "opening_style", "clip_purpose",
        "pacing", "payoff_type", "caption_style",
    ))
    assert not annotations.exists(reference_id)
    assert audit.history() == []


def test_annotation_update_stale_guard_audit_and_reanalysis_survival(tmp_path):
    _, entries, annotations, audit, _, service = environment(tmp_path)
    reference_id = entries[0]["reference_id"]
    values = annotation_values(
        composition="full_screen_gameplay",
        facecam_presence="none",
        opening_style="immediate_action",
        clip_purpose="clutch_highlight",
        pacing="fast",
        payoff_type="gameplay_result",
        caption_style="phrase_captions",
        desired_qualities=["complete result"],
        undesirable_qualities=["dead air"],
        reviewer_notes="Keep the final play.",
    )
    updated = service.update_annotations(
        reference_id, expected_revision=0, values=values,
        request_id="annotation-one",
    )
    assert updated["revision"] == 1 and updated["reviewer"] == "Fixture reviewer"
    before = annotations.path(reference_id).read_bytes()
    before_audit = audit.path.read_bytes()
    with pytest.raises(ReferenceAnnotationError, match="Stale"):
        service.update_annotations(
            reference_id, expected_revision=0, values=values,
            request_id="annotation-stale",
        )
    assert annotations.path(reference_id).read_bytes() == before
    assert audit.path.read_bytes() != before_audit
    stale_event = audit.history(reference_id=reference_id)[-1]
    assert stale_event["result"] == "failure"
    assert stale_event["failure_reason"] == "Stale annotation revision."
    service.reanalyze(
        reference_id, transcription=True, force=True, request_id="reanalyze-after-note"
    )
    assert annotations.path(reference_id).read_bytes() == before
    event = audit.history(reference_id=reference_id)[0]
    assert event["changed_fields"] == sorted(ANNOTATION_FIELDS)
    serialized = json.dumps(event).casefold()
    for forbidden in ("form_token", "api_key", "cookie", "authorization"):
        assert forbidden not in serialized


def test_profile_separates_evidence_counts_and_staleness(tmp_path):
    _, entries, _, audit, builder, service = environment(tmp_path)
    for index in (0, 1):
        service.update_annotations(
            entries[index]["reference_id"], expected_revision=0,
            values=annotation_values(
                composition="full_screen_gameplay",
                pacing="fast",
                clip_purpose="clutch_highlight",
            ),
            request_id=f"annotation-{index}",
        )
    profile = service.rebuild_profile(
        "gaming_highlight", request_id="profile-one"
    )
    assert profile["version"] == 3 and profile["staleness"]["status"] == "current"
    assert profile["built_at"] and profile["category"] == "gaming_highlight"
    assert profile["automatic_evidence"]["duration"]["contributor_count"] == 3
    composition = profile["human_preferences"]["fields"]["composition"]
    assert composition["contributor_count"] == 2
    assert composition["value_counts"] == {"full_screen_gameplay": 2}
    assert profile["human_preferences"]["fields"]["facecam_presence"]["evidence_kind"] == "unavailable"
    assert all(set(value) == {
        "reference_id", "analysis_revision", "annotation_revision"
    } for value in profile["input_versions"])
    reference_id = entries[0]["reference_id"]
    service.reanalyze(
        reference_id, transcription=True, force=True, request_id="profile-stale-analysis"
    )
    stale = builder.read("gaming_highlight")
    assert stale["staleness"]["status"] == "stale"
    assert any("analysis revision" in reason for reason in stale["staleness"]["reasons"])
    rebuilt = service.rebuild_profile(
        "gaming_highlight", request_id="profile-two"
    )
    assert builder.read("gaming_highlight")["staleness"]["status"] == "current"
    service.update_annotations(
        reference_id, expected_revision=1,
        values=annotation_values(pacing="very_fast"),
        request_id="profile-stale-annotation",
    )
    stale = builder.read("gaming_highlight")
    assert any("annotation revision" in reason for reason in stale["staleness"]["reasons"])
    assert rebuilt["reference_ids"] == sorted(
        entry["reference_id"] for entry in entries
    )
    assert audit.history(profile_name="gaming_highlight")[-2]["action"] == "profile_rebuild"


def test_profile_v3_complete_timing_aggregates_missing_and_mixed_preferences(tmp_path):
    _, entries, _, _, builder, service = environment(tmp_path)
    openings = ("immediate_action", "spoken_hook", "mid_action_opening")
    for index, entry in enumerate(entries):
        service.update_annotations(
            entry["reference_id"], expected_revision=0,
            values=annotation_values(
                opening_style=openings[index], payoff_type=(
                    "gameplay_result", "reaction", "punchline"
                )[index], pacing="fast",
            ), request_id=f"timing-annotation-{index}",
        )
        service.reanalyze(
            entry["reference_id"], transcription=True, force=True,
            request_id=f"timing-analysis-{index}",
        )
    # One unavailable hook must be excluded, not converted to zero.
    analysis_path = Path(entries[2]["analysis_path"])
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    analysis["speech"]["likely_hook"]["timestamp"] = None
    analysis_path.write_text(json.dumps(analysis), encoding="utf-8")
    unrelated = builder.output_directory / "personality_reaction.json"
    unrelated.parent.mkdir(parents=True, exist_ok=True)
    unrelated.write_bytes(b'{"fixture":"unchanged"}\n')
    profile = builder.build("gaming_highlight")
    assert profile["version"] == 3 and profile["built_at"]
    assert profile["category"] == "gaming_highlight"
    automatic = profile["automatic_evidence"]
    for name in (
        "words_per_spoken_second", "words_per_media_second", "speech_density",
        "speech_start", "hook_timing", "payoff_timing", "post_payoff_tail",
        "post_speech_tail", "unresolved_ending", "question_count", "reaction_count",
    ):
        assert {"contributor_count", "unavailable_count", "median", "range",
                "evidence_type"} <= set(automatic[name])
    assert automatic["hook_timing"]["contributor_count"] == 2
    assert automatic["hook_timing"]["unavailable_count"] == 1
    assert automatic["hook_timing"]["range"] == [0.4, 0.4]
    assert automatic["payoff_timing"]["median"] == 40.6
    assert automatic["post_payoff_tail"]["median"] == 4.4
    assert automatic["post_speech_tail"]["median"] == 4.4
    fields = profile["human_preferences"]["fields"]
    assert fields["opening_style"]["summary"]["status"] == "mixed"
    assert fields["payoff_type"]["summary"]["status"] == "mixed"
    assert unrelated.read_bytes() == b'{"fixture":"unchanged"}\n'


def test_profile_loading_remains_backward_compatible(tmp_path):
    _, _, _, _, builder, _ = environment(tmp_path, count=1)
    current = builder.build("gaming_highlight")
    path = builder.profile_path("gaming_highlight")
    version_two = dict(current)
    version_two["version"] = 2
    version_two.pop("built_at")
    version_two.pop("category")
    path.write_text(json.dumps(version_two), encoding="utf-8")
    assert builder.read("gaming_highlight")["version"] == 2
    path.write_text(json.dumps({
        "version": 1, "profile_name": "gaming_highlight", "reference_ids": []
    }), encoding="utf-8")
    assert builder.read("gaming_highlight")["version"] == 1


def test_audit_append_failure_rolls_back_annotations_analysis_and_profiles(tmp_path):
    library, entries, annotations, _, builder, _ = environment(tmp_path)
    reference_id = entries[0]["reference_id"]
    builder.build("gaming_highlight")
    profile_path = builder.profile_path("gaming_highlight")
    profile_before = profile_path.read_bytes()
    analysis_path = Path(entries[0]["analysis_path"])
    analysis_before = analysis_path.read_bytes()
    failing = ReferenceEvidenceService(
        library,
        annotations=annotations,
        audit=FailingAuditLedger(tmp_path / "failing-events.jsonl"),
        profile_builder=builder,
        analyzer_factory=lambda value: ReferenceClipAnalyzer(
            value, runner=Runner(), transcriber=Transcriber()
        ),
        lock_path=tmp_path / "reference_annotations" / ".lock",
    )
    with pytest.raises(ReferenceEvidenceAuditError, match="durable append"):
        failing.update_annotations(
            reference_id, expected_revision=0,
            values=annotation_values(pacing="fast"),
            request_id="failed-annotation-audit",
        )
    assert not annotations.exists(reference_id)
    assert profile_path.read_bytes() == profile_before

    with pytest.raises(ReferenceEvidenceAuditError, match="durable append"):
        failing.reanalyze(
            reference_id, transcription=True, force=True,
            request_id="failed-reanalysis-audit",
        )
    assert analysis_path.read_bytes() == analysis_before
    assert profile_path.read_bytes() == profile_before


def test_cached_analysis_does_not_mark_profile_stale(tmp_path):
    _, entries, _, audit, builder, service = environment(tmp_path)
    reference_id = entries[0]["reference_id"]
    service.rebuild_profile("gaming_highlight", request_id="initial-profile")
    before = builder.profile_path("gaming_highlight").read_bytes()
    result = service.reanalyze(
        reference_id, transcription=True, force=False,
        request_id="cached-reanalysis",
    )
    assert result["analysis_revision"] == 1
    assert builder.profile_path("gaming_highlight").read_bytes() == before
    assert builder.read("gaming_highlight")["staleness"]["status"] == "current"
    event = audit.history(reference_id=reference_id)[-1]
    assert event["previous_analysis_revision"] == event["new_analysis_revision"] == 1


def test_profile_excludes_nonaccepted_and_removed_references(tmp_path):
    library, entries, annotations, _, builder, service = environment(tmp_path)
    root = library.root
    rejected_id = "youtube-rejected"
    rejected = root / "rejected"
    rejected.mkdir()
    (rejected / "reference.mp4").write_bytes(b"rejected")
    (rejected / "baseline.json").write_text(
        json.dumps(baseline(rejected_id, status="rejected")), encoding="utf-8"
    )
    entry = library.register_directory(rejected, profile_name="gaming_highlight")
    ReferenceClipAnalyzer(library, runner=Runner()).analyze(
        entry["reference_id"], transcription=False
    )
    removed_id = entries[-1]["reference_id"]
    library.remove(removed_id)
    profile = service.rebuild_profile(
        "gaming_highlight", request_id="profile-exclusions"
    )
    assert rejected_id not in profile["reference_ids"]
    assert removed_id not in profile["reference_ids"]
    assert set(profile["reference_ids"]) == {
        entries[0]["reference_id"], entries[1]["reference_id"]
    }
    assert not (tmp_path / "published").exists()
    assert annotations.root.is_relative_to(tmp_path)


def test_legacy_analysis_revision_is_compatible(tmp_path):
    _, entries, _, _, builder, _ = environment(tmp_path, count=1)
    path = Path(entries[0]["analysis_path"])
    document = json.loads(path.read_text())
    document["version"] = 1
    document.pop("analysis_revision")
    document.pop("updated_at")
    document.pop("transcription")
    path.write_text(json.dumps(document), encoding="utf-8")
    profile = builder.build("gaming_highlight")
    assert profile["input_versions"][0]["analysis_revision"] == 0
    assert builder.read("gaming_highlight")["staleness"]["status"] == "current"


def test_cli_annotation_history_rebuild_and_transcription_flags(
    tmp_path, capsys, monkeypatch
):
    library, entries, annotations, audit, builder, _ = environment(tmp_path)
    common = [
        "--reference-root", str(library.root),
        "--index-path", str(library.index_path),
        "--profile-directory", str(builder.output_directory),
        "--annotation-directory", str(annotations.root),
        "--evidence-audit-path", str(audit.path),
    ]
    reference_id = entries[0]["reference_id"]
    assert reference_clips.main(common + [
        "annotate", reference_id, "--expected-revision", "0",
        "--composition", "full_screen_gameplay",
        "--desired-quality", "complete play",
        "--reviewer-notes", "fixture note",
    ]) == 0
    assert reference_clips.main(common + ["show-annotations", reference_id]) == 0
    assert reference_clips.main(common + ["build-profile", "gaming_highlight"]) == 0
    assert reference_clips.main(common + ["evidence-history", reference_id]) == 0
    output = capsys.readouterr().out
    assert "revision 1" in output and "annotation_update" in output

    calls = []

    def fake_reanalyze(self, value, **kwargs):
        calls.append((value, kwargs))
        existing = json.loads(Path(library.get(value)["analysis_path"]).read_text())
        existing["transcription"] = {"status": "available"}
        return existing

    monkeypatch.setattr(ReferenceEvidenceService, "reanalyze", fake_reanalyze)
    assert reference_clips.main(common + [
        "analyze", "--reference-id", reference_id,
        "--with-transcription", "--force",
    ]) == 0
    assert calls[0][0] == reference_id
    assert calls[0][1]["transcription"] is True
    assert calls[0][1]["force"] is True
