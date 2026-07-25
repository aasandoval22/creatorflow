import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app import reference_clips
from backend.services.reference_clip_analyzer import (
    ReferenceAnalysisError, ReferenceClipAnalyzer, parse_scene_changes,
    parse_silence_intervals,
)
from backend.services.reference_clip_comparator import ReferenceClipComparator
from backend.services.reference_clip_library import (
    ReferenceClipError, ReferenceClipLibrary, load_and_validate_baseline,
)
from backend.services.reference_profile_builder import (
    ReferenceProfileBuilder, ReferenceProfileError,
)


def baseline(reference_id="youtube-video", duration_note="local"):
    return {
        "version": 1, "reference_id": reference_id, "source_video_id": "video",
        "source_title": "Title", "creator": "Creator", "status": "accepted",
        "purpose": "creatorflow_baseline",
        "qualities": ["funny", "strong streamer personality"],
        "layout": {"orientation": "vertical", "top_region": "facecam",
                   "bottom_region": "gameplay"},
        "timing_preferences": {
            "requires_long_lead_in": False, "requires_complete_setup": False,
            "requires_complete_payoff": True,
            "preferred_ending": "immediately after the final funny line",
        },
        "notes": duration_note,
    }


def reference_dir(tmp_path, name="one", *, reference_id="youtube-video", content=b"media"):
    directory = tmp_path / name
    directory.mkdir(parents=True)
    (directory / "reference.mp4").write_bytes(content)
    (directory / "baseline.json").write_text(
        json.dumps(baseline(reference_id)), encoding="utf-8"
    )
    (directory / "reference.info.json").write_text("{}", encoding="utf-8")
    return directory


def library_with_reference(tmp_path, **kwargs):
    root = tmp_path / "refs"
    directory = reference_dir(root, **kwargs)
    library = ReferenceClipLibrary(root)
    entry = library.register_directory(directory, profile_name="personality_reaction")
    return library, entry, directory


def probe(duration=44.881):
    return {
        "streams": [
            {"codec_type": "video", "codec_name": "av1", "width": 1080, "height": 1920,
             "r_frame_rate": "60/1", "avg_frame_rate": "60000/1000",
             "display_aspect_ratio": "9:16"},
            {"codec_type": "audio", "codec_name": "opus", "sample_rate": "48000"},
        ],
        "format": {"duration": str(duration)},
    }


class Runner:
    def __init__(self, probe_value=None, fail=None):
        self.probe_value = probe_value or probe()
        self.fail = fail
        self.calls = []

    def __call__(self, command):
        self.calls.append(command)
        if self.fail and self.fail in command[0]:
            return SimpleNamespace(returncode=1, stdout="", stderr="synthetic failure")
        if "-show_streams" in command:
            return SimpleNamespace(returncode=0, stdout=json.dumps(self.probe_value), stderr="")
        if "select=" in " ".join(command):
            return SimpleNamespace(returncode=0, stdout="", stderr="n:1 pts:60 pts_time:1.5\nn:2 pts_time:3")
        return SimpleNamespace(
            returncode=0, stdout="",
            stderr="[silencedetect] silence_start: 0\n[silencedetect] silence_end: 0.5 | silence_duration: 0.5",
        )


class Transcriber:
    def transcribe(self, *_args, **_kwargs):
        words = [
            SimpleNamespace(word="What?", start=0.4, end=0.8),
            SimpleNamespace(word="No", start=1.7, end=1.9),
            SimpleNamespace(word="way!", start=2.0, end=2.3),
            SimpleNamespace(word="because", start=3.0, end=3.3),
        ]
        return iter([SimpleNamespace(words=words)]), SimpleNamespace(language="en")


def analyze_reference(tmp_path, *, transcriber=None, duration=44.881):
    library, entry, _ = library_with_reference(tmp_path)
    analyzer = ReferenceClipAnalyzer(
        library, runner=Runner(probe(duration)), transcriber=transcriber
    )
    return library, entry, analyzer.analyze(
        entry["reference_id"], transcription=transcriber is not None
    )


def test_register_autodiscovery_checksum_filter_and_remove_preserves_media(tmp_path):
    library, entry, directory = library_with_reference(tmp_path)
    assert entry["reference_id"] == "youtube-video"
    assert entry["source_info_path"].endswith("reference.info.json")
    assert entry["checksum_sha256"] == hashlib.sha256(b"media").hexdigest()
    assert library.validate_checksum(entry["reference_id"])
    assert library.list_references(status="accepted", creator="creator",
                                   profile_name="personality_reaction") == [entry]
    removed = library.remove(entry["reference_id"])
    assert removed == entry and (directory / "reference.mp4").is_file()


def test_explicit_paths_stable_id_and_streaming_large_media(tmp_path):
    directory = reference_dir(tmp_path, content=b"x" * (2 * 1024 * 1024 + 3))
    library = ReferenceClipLibrary(tmp_path / "index-root")
    entry = library.register(
        media_path=directory / "reference.mp4",
        baseline_path=directory / "baseline.json",
        source_info_path=directory / "reference.info.json",
    )
    assert library.stable_reference_id(
        load_and_validate_baseline(directory / "baseline.json"),
        directory / "reference.mp4",
    ) == entry["reference_id"]


@pytest.mark.parametrize("missing", ["reference.mp4", "baseline.json"])
def test_missing_required_files(tmp_path, missing):
    directory = reference_dir(tmp_path)
    (directory / missing).unlink()
    with pytest.raises(ReferenceClipError, match="does not exist"):
        ReferenceClipLibrary(tmp_path / "root").register_directory(directory)


def test_invalid_baseline_duplicates_changed_checksum_and_corrupt_index(tmp_path):
    directory = reference_dir(tmp_path)
    (directory / "baseline.json").write_text('{"version":1}', encoding="utf-8")
    with pytest.raises(ReferenceClipError, match="missing"):
        ReferenceClipLibrary(tmp_path / "root").register_directory(directory)
    library, entry, directory = library_with_reference(tmp_path / "valid")
    with pytest.raises(ReferenceClipError, match="already registered"):
        library.register_directory(directory)
    (directory / "reference.mp4").write_bytes(b"changed")
    with pytest.raises(ReferenceClipError, match="changed"):
        library.validate_checksum(entry["reference_id"])
    library.index_path.write_text("{", encoding="utf-8")
    with pytest.raises(ReferenceClipError, match="corrupt"):
        library.list_references()


def test_duplicate_media_identity_with_different_id(tmp_path):
    library, _, directory = library_with_reference(tmp_path)
    changed = baseline("youtube-other")
    (directory / "baseline.json").write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ReferenceClipError, match="already registered"):
        library.register_directory(directory)


def test_update_annotations_preserves_qualities_and_atomic_files(tmp_path):
    library, entry, directory = library_with_reference(tmp_path)
    document = baseline()
    document["notes"] = "<user & note>"
    document["qualities"] = ["my exact words"]
    (directory / "baseline.json").write_text(json.dumps(document), encoding="utf-8")
    library.update_annotations(entry["reference_id"])
    assert load_and_validate_baseline(directory / "baseline.json")["qualities"] == ["my exact words"]
    assert not list(library.index_path.parent.glob(".index.json.*.tmp"))


def test_probe_scene_silence_and_no_transcription(tmp_path):
    library, entry, analysis = analyze_reference(tmp_path)
    media = analysis["media"]
    assert (media["duration"], media["width"], media["height"]) == (44.881, 1080, 1920)
    assert (media["frame_rate"], media["video_codec"], media["audio_codec"]) == (60.0, "av1", "opus")
    assert analysis["speech"]["word_count"] == 0
    assert analysis["visual_timing"]["scene_change_timestamps"] == [1.5, 3.0]
    assert analysis["audio_timing"]["silence_intervals"][0]["duration"] == 0.5
    assert Path(entry["analysis_path"]).is_file()
    assert not list(Path(entry["analysis_path"]).parent.glob(".analysis.json.*.tmp"))


def test_transcription_speech_question_reaction_payoff_metrics(tmp_path):
    _, _, analysis = analyze_reference(tmp_path, transcriber=Transcriber())
    speech = analysis["speech"]
    assert speech["language"] == "en" and speech["word_count"] == 4
    assert speech["first_word_start"] == 0.4 and speech["last_word_end"] == 3.3
    assert speech["post_speech_tail"] == 41.581
    assert speech["questions"] and "no way" in speech["reaction_signals"]
    assert "because" in speech["payoff_signals"] and speech["meaningful_pauses"]


@pytest.mark.parametrize("value,message", [
    ({"streams": [], "format": {"duration": "1"}}, "no video"),
    ({"streams": "bad"}, "streams"),
])
def test_malformed_or_missing_probe_streams(tmp_path, value, message):
    library, entry, _ = library_with_reference(tmp_path)
    analyzer = ReferenceClipAnalyzer(library, runner=Runner(value))
    with pytest.raises(ReferenceAnalysisError, match=message):
        analyzer.analyze(entry["reference_id"], transcription=False)
    assert not Path(entry["analysis_path"]).exists()


def test_parsers_are_deterministic():
    assert parse_scene_changes("pts_time:2\npts_time:1\npts_time:2") == [1.0, 2.0]
    assert parse_silence_intervals(
        "silence_start: 2.0\nsilence_end: 3.5 | silence_duration: 1.5"
    ) == [{"start": 2.0, "end": 3.5, "duration": 1.5}]


def test_analyzer_failure_cleanup(tmp_path):
    library, entry, _ = library_with_reference(tmp_path)
    with pytest.raises(ReferenceAnalysisError, match="synthetic"):
        ReferenceClipAnalyzer(library, runner=Runner(fail="ffprobe")).analyze(
            entry["reference_id"], transcription=False
        )
    assert not Path(entry["analysis_path"]).exists()


def test_profile_one_reference_is_provisional_and_deterministic(tmp_path):
    library, _, _ = analyze_reference(tmp_path)
    builder = ReferenceProfileBuilder(library, tmp_path / "profiles")
    first = builder.build("personality_reaction")
    second = builder.build("personality_reaction")
    assert first == second
    assert first["confidence"] == "provisional" and first["reference_count"] == 1
    assert first["duration"]["observed_median"] == 44.881
    assert first["duration"]["recommended_minimum"] < 44.881 < first["duration"]["recommended_maximum"]
    assert first["opening"]["mid_action_allowed"] and first["story"]["payoff_required"]
    assert first["layout"]["preferred_composition"] == "stacked_facecam_gameplay"


def test_profile_multiple_references_median_and_ids(tmp_path):
    root = tmp_path / "refs"
    library = ReferenceClipLibrary(root)
    for index, duration in enumerate((30.0, 60.0)):
        directory = reference_dir(root, f"r{index}", reference_id=f"ref-{index}", content=bytes([index]))
        entry = library.register_directory(directory, profile_name="personality_reaction")
        ReferenceClipAnalyzer(library, runner=Runner(probe(duration))).analyze(
            entry["reference_id"], transcription=False
        )
    profile = ReferenceProfileBuilder(library, tmp_path / "profiles").build("personality_reaction")
    assert profile["confidence"] == "multi_reference"
    assert profile["duration"]["observed_median"] == 45.0
    assert profile["reference_ids"] == ["ref-0", "ref-1"]


def comparison_setup(tmp_path):
    library, _, _ = analyze_reference(tmp_path / "reference")
    builder = ReferenceProfileBuilder(library, tmp_path / "profiles")
    builder.build("personality_reaction")
    return ReferenceClipComparator(builder, tmp_path / "reports")


def review(**changes):
    value = {
        "review_id": "review_abc", "video_id": "video", "status": "rejected",
        "render_start": 10.0, "render_end": 55.0, "render_duration": 45.0,
        "candidate_start": 15.0, "candidate_end": 50.0,
    }
    value.update(changes)
    return value


def transcript(*words):
    return {"segments": [{"words": [
        {"word": text, "start": start, "end": end} for text, start, end in words
    ]}]}


@pytest.mark.parametrize("duration,status", [(45, "within_profile"), (10, "too_short"), (90, "too_long")])
def test_comparator_duration_and_human_evidence(tmp_path, duration, status):
    report = comparison_setup(tmp_path).compare(
        "personality_reaction", review(render_duration=duration), transcript={}
    )
    finding = report["findings"]["duration_fit"]
    assert finding["status"] == status and finding["evidence"]
    assert "humor" in report["limitations"] and "virality" in report["limitations"]


def test_comparator_opening_payoff_tail_layout_caption_and_containment(tmp_path):
    comparator = comparison_setup(tmp_path)
    words = transcript(
        ("Earlier.", 1, 2), ("What?", 20, 21), ("because", 22, 23),
        ("Done!", 52, 53),
    )
    metadata = {
        "layout": "stacked_facecam_gameplay", "caption_configuration": {"font_size": 62}
    }
    report = comparator.compare(
        "personality_reaction", review(render_end=60), metadata=metadata,
        transcript=words, write=True
    )
    findings = report["findings"]
    assert findings["opening_context"]["status"] == "possibly_late"
    assert findings["payoff_completion"]["status"] == "likely_complete"
    assert findings["ending_tail"]["status"] == "excessive"
    assert findings["layout"]["status"] == "matching"
    assert findings["captions"]["status"] == "present"
    assert findings["candidate_containment"]["status"] == "contained"
    assert comparator.report_path("personality_reaction", "review_abc").is_file()


def test_comparator_unresolved_abrupt_short_tail_different_and_missing(tmp_path):
    comparator = comparison_setup(tmp_path)
    unresolved = comparator.compare(
        "personality_reaction", review(render_end=20.2),
        metadata={"layout": "other"},
        transcript=transcript(("What?", 19, 20)),
    )["findings"]
    assert unresolved["payoff_completion"]["status"] == "unresolved"
    assert unresolved["ending_tail"]["status"] == "short"
    assert unresolved["layout"]["status"] == "different"
    missing = comparator.compare(
        "personality_reaction", {"review_id": "review_missing"},
        metadata={}, transcript={},
    )["findings"]
    assert all(missing[key]["evidence_kind"] == "unavailable"
               for key in ("opening_context", "ending_tail", "candidate_containment"))


def test_comparator_not_contained_and_complete_sentence(tmp_path):
    findings = comparison_setup(tmp_path).compare(
        "personality_reaction",
        review(render_start=16, render_end=49),
        transcript=transcript(("Finished.", 20, 48)),
    )["findings"]
    assert findings["candidate_containment"]["status"] == "not_contained"
    assert findings["payoff_completion"]["status"] == "no_unresolved_signal"


def test_cli_register_list_show_validate_build_and_explicit_argv(tmp_path, capsys):
    directory = reference_dir(tmp_path / "refs")
    common = ["--reference-root", str(tmp_path / "refs"), "--index-path", str(tmp_path / "index.json"),
              "--profile-directory", str(tmp_path / "profiles")]
    assert reference_clips.main(common + ["register", "--reference-directory", str(directory),
                                          "--profile", "personality_reaction"]) == 0
    assert reference_clips.main(common + ["list"]) == 0
    assert reference_clips.main(common + ["show", "youtube-video"]) == 0
    assert reference_clips.main(common + ["validate", "youtube-video"]) == 0
    assert "checksum valid" in capsys.readouterr().out
    assert reference_clips.main(common + ["show", "missing"]) == 1


def test_cli_invalid_arguments():
    with pytest.raises(SystemExit):
        reference_clips.main(["register"])
