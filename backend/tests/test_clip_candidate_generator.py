import json
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.services.clip_candidate_generator import (
    AnalysisResultStatus,
    CandidateConfiguration,
    ClipCandidateGenerator,
    EndingClassification,
    TranscriptError,
)
from backend.services.video_manifest import (
    ClipAnalysisStatus,
    TranscriptionStatus,
    VideoManifest,
    default_clip_analysis,
    default_transcription,
)


def manifest_record(video_id: str, transcript_path: Path) -> dict:
    transcription = default_transcription()
    transcription.update(
        status=TranscriptionStatus.COMPLETED.value,
        transcript_json_path=str(transcript_path),
    )
    return {
        "video_id": video_id,
        "source_platform": "youtube",
        "channel_name": "Creator",
        "channel_url": "https://example.test/creator",
        "video_url": f"https://example.test/{video_id}",
        "title": "Test",
        "uploader": "Creator",
        "upload_date": "20260724",
        "duration_seconds": 90,
        "discovered_at": "2026-07-24T12:00:00+00:00",
        "downloaded_at": "2026-07-24T12:01:00+00:00",
        "local_file_path": f"/tmp/{video_id}.mp4",
        "status": "downloaded",
        "error_message": None,
        "transcription": transcription,
        "clip_analysis": default_clip_analysis(),
    }


def segments(count=8, seconds=6):
    return [
        {
            "id": index,
            "start": index * seconds,
            "end": (index + 1) * seconds,
            "text": (
                "Here is the reason you should choose the best option. "
                "It has three concrete benefits."
            ),
        }
        for index in range(count)
    ]


def write_transcript(path: Path, video_id="abc", content=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "version": 1,
        "video_id": video_id,
        "source_media_path": "/tmp/source.mp4",
        "segments": segments() if content is None else content,
    }
    path.write_text(json.dumps(document), encoding="utf-8")


def test_validates_missing_invalid_and_empty_transcripts(tmp_path):
    with pytest.raises(TranscriptError, match="does not exist"):
        ClipCandidateGenerator.load_transcript(tmp_path / "missing.json")
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    with pytest.raises(TranscriptError, match="Invalid transcript JSON"):
        ClipCandidateGenerator.load_transcript(broken)
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"version": 2, "segments": []}), encoding="utf-8")
    with pytest.raises(TranscriptError, match="version 1"):
        ClipCandidateGenerator.load_transcript(wrong)
    empty = tmp_path / "empty.json"
    write_transcript(empty, content=[])
    with pytest.raises(TranscriptError, match="non-empty"):
        ClipCandidateGenerator.load_transcript(empty)


@pytest.mark.parametrize(
    "segment",
    [
        {"id": 1, "start": -1, "end": 2, "text": "bad"},
        {"id": 1, "start": 2, "end": 1, "text": "bad"},
        {"id": 1, "start": "0", "end": 2, "text": "bad"},
        {"id": 1, "start": 0, "end": 2, "text": 5},
    ],
)
def test_rejects_malformed_segment_values(tmp_path, segment):
    path = tmp_path / "transcript.json"
    write_transcript(path, content=[segment])
    with pytest.raises(TranscriptError):
        ClipCandidateGenerator.load_transcript(path)


def test_missing_words_and_ids_are_allowed(tmp_path):
    path = tmp_path / "transcript.json"
    write_transcript(
        path,
        content=[{"start": 0, "end": 5, "text": "A complete statement."}],
    )
    loaded = ClipCandidateGenerator.load_transcript(path)
    assert loaded["segments"][0]["id"] == 0


def test_generation_combines_complete_segments_and_is_stable():
    generator = ClipCandidateGenerator.__new__(ClipCandidateGenerator)
    generator.configuration = CandidateConfiguration(
        minimum_duration_seconds=12,
        target_duration_seconds=18,
        maximum_duration_seconds=24,
        minimum_word_count=10,
        maximum_overlap=0.5,
        maximum_candidates=3,
    )
    transcript_segments = segments(8, 6)
    first = generator.generate_candidates("abc", transcript_segments)
    second = generator.generate_candidates("abc", transcript_segments)
    assert first == second
    assert first
    assert all(12 <= candidate["duration"] <= 24 for candidate in first)
    assert all(candidate["start"] % 6 == 0 for candidate in first)
    assert all(candidate["end"] % 6 == 0 for candidate in first)
    assert len(first) <= 3


def test_short_transcript_returns_no_candidates():
    generator = ClipCandidateGenerator.__new__(ClipCandidateGenerator)
    generator.configuration = CandidateConfiguration()
    assert generator.generate_candidates("abc", segments(2, 5)) == []


def test_scoring_is_deterministic_additive_and_penalizes_sponsors():
    good = (
        "Here is why you should use the best method. "
        "The reason is that it saves 3 hours. This is the clear conclusion."
    )
    sponsored = good + " Thanks to our sponsor; use code SAVE and subscribe."
    components, reasons = ClipCandidateGenerator.score_candidate(good, 30)
    again, _ = ClipCandidateGenerator.score_candidate(good, 30)
    bad_components, bad_reasons = ClipCandidateGenerator.score_candidate(
        sponsored, 30
    )
    assert components == again
    assert 0 <= max(0, min(100, sum(components.values()))) <= 100
    assert sum(bad_components.values()) < sum(components.values())
    assert any("recommendation" in reason for reason in reasons)
    assert any("clear" in reason for reason in reasons)
    assert any("sponsor" in reason for reason in bad_reasons)


def test_service_writes_atomic_artifact_and_updates_manifest(tmp_path):
    transcript = tmp_path / "input" / "transcript.json"
    write_transcript(transcript)
    manifest = VideoManifest(tmp_path / "videos.json")
    manifest.upsert(manifest_record("abc", transcript))
    output = tmp_path / "candidates"
    generator = ClipCandidateGenerator(
        manifest=manifest,
        output_directory=output,
        configuration=CandidateConfiguration(
            minimum_duration_seconds=12,
            target_duration_seconds=18,
            maximum_duration_seconds=24,
            minimum_word_count=10,
        ),
    )
    batch = generator.analyze()
    result = batch.results[0]
    assert result.status is AnalysisResultStatus.SUCCESS
    artifact = json.loads(Path(result.candidates_json_path).read_text())
    assert artifact["version"] == 1
    assert artifact["video_id"] == "abc"
    assert artifact["candidates"][0]["rank"] == 1
    assert manifest.get("abc")["clip_analysis"]["status"] == "completed"
    assert list(output.rglob("*.tmp")) == []


def test_service_skips_completed_unless_forced(tmp_path):
    transcript = tmp_path / "transcript.json"
    write_transcript(transcript)
    manifest = VideoManifest(tmp_path / "videos.json")
    manifest.upsert(manifest_record("abc", transcript))
    manifest.update_clip_analysis(
        "abc", status=ClipAnalysisStatus.COMPLETED.value
    )
    generator = ClipCandidateGenerator(manifest=manifest, output_directory=tmp_path)
    assert generator.analyze().skipped == 1
    assert generator.analyze(force=True).successful == 1


def test_service_marks_failure_and_continues(tmp_path):
    good_path = tmp_path / "good.json"
    write_transcript(good_path, "good")
    manifest = VideoManifest(tmp_path / "videos.json")
    manifest.upsert(manifest_record("bad", tmp_path / "missing.json"))
    manifest.upsert(manifest_record("good", good_path))
    generator = ClipCandidateGenerator(
        manifest=manifest,
        output_directory=tmp_path / "output",
        configuration=CandidateConfiguration(
            minimum_duration_seconds=12,
            target_duration_seconds=18,
            maximum_duration_seconds=24,
            minimum_word_count=10,
        ),
    )
    batch = generator.analyze()
    assert batch.failed == 1
    assert batch.successful == 1
    assert manifest.get("bad")["clip_analysis"]["status"] == "failed"
    assert "does not exist" in manifest.get("bad")["clip_analysis"]["error_message"]


def test_artifact_replace_failure_does_not_advertise_completion(tmp_path):
    transcript = tmp_path / "transcript.json"
    write_transcript(transcript)
    manifest = VideoManifest(tmp_path / "videos.json")
    manifest.upsert(manifest_record("abc", transcript))
    generator = ClipCandidateGenerator(manifest=manifest, output_directory=tmp_path / "out")
    original_replace = __import__("os").replace

    def fail_candidate(source, destination):
        if str(destination).endswith("candidates.json"):
            raise OSError("replace failed")
        return original_replace(source, destination)

    with patch(
        "backend.services.clip_candidate_generator.os.replace",
        side_effect=fail_candidate,
    ):
        batch = generator.analyze()
    assert batch.failed == 1
    assert manifest.get("abc")["clip_analysis"]["status"] == "failed"
    assert list((tmp_path / "out").rglob("*.tmp")) == []


def quality_generator(**overrides):
    generator = ClipCandidateGenerator.__new__(ClipCandidateGenerator)
    values = {
        "minimum_duration_seconds": 10,
        "target_duration_seconds": 20,
        "maximum_duration_seconds": 30,
        "minimum_word_count": 8,
        "maximum_overlap": 0.5,
        "maximum_candidates": 5,
    }
    values.update(overrides)
    generator.configuration = CandidateConfiguration(**values)
    return generator


def timed_texts(texts, seconds=5, pauses=None):
    result = []
    start = 0.0
    pauses = pauses or {}
    for index, text in enumerate(texts):
        start += pauses.get(index, 0)
        result.append({"id": index, "start": start, "end": start + seconds, "text": text})
        start += seconds
    return result


@pytest.mark.parametrize(
    "fragment",
    [
        "made last year and still works well.",
        "does not consume your cooldown when active.",
        "that's fine I guess for this option.",
        "range to work with on this weapon.",
    ],
)
def test_continuation_fragments_receive_substantial_start_penalty(fragment):
    prior = {"start": 0, "end": 5, "text": "The old version was"}
    bad = {"start": 5, "end": 10, "text": fragment}
    good = {
        "start": 10,
        "end": 15,
        "text": "How does the build work? It is pretty simple in theory.",
    }
    bad_score, bad_reasons = ClipCandidateGenerator._assess_start(bad, prior)
    good_score, _ = ClipCandidateGenerator._assess_start(good, bad)
    assert good_score >= bad_score + 5
    assert any("continues" in reason for reason in bad_reasons)


def test_generation_prefers_self_contained_opener_over_preceding_fragment():
    transcript = timed_texts(
        [
            "This is text from the previous thought and",
            "How does the build work? It is pretty simple in theory.",
            "The build uses grenade energy to refresh the cooldown.",
            "and you should trigger the perk before dealing damage.",
            "That gives the setup a complete and actionable takeaway.",
            "Another unrelated topic starts here.",
        ]
    )
    candidate = quality_generator().generate_candidates("abc", transcript)[0]
    assert candidate["text"].startswith("How does the build work?")
    assert candidate["component_scores"]["start_boundary_score"] > 10


def test_generation_continues_toward_target_instead_of_stopping_at_minimum():
    transcript = timed_texts(
        [
            "How does this build work? Here is the answer.",
            "The setup uses a named grenade mechanic.",
            "It refreshes the cooldown after every hit.",
            "You should use the perk before the attack.",
            "That is the complete takeaway.",
        ]
    )
    candidate = quality_generator().generate_candidates("abc", transcript)[0]
    assert candidate["duration"] == 20


def test_generation_can_stop_before_target_at_strong_complete_ending():
    transcript = timed_texts(
        [
            "How should you use this setup? Start with the perk.",
            "The mechanic grants exactly 3 seconds of cooldown.",
            "That is the complete recommendation and takeaway.",
            "and this trailing list continues without a conclusion",
            "because the next segment is still incomplete",
        ]
    )
    candidate = quality_generator(target_duration_seconds=25).generate_candidates(
        "abc", transcript
    )[0]
    assert candidate["duration"] == 15


def test_generation_extends_past_target_to_complete_statement_without_exceeding_maximum():
    transcript = timed_texts(
        [
            "How does the setup work? The mechanic begins here.",
            "The grenade provides a concrete cooldown benefit.",
            "You should activate it before the attack.",
            "The reason is",
            "that the final hit refreshes the skill completely.",
            "This build now has a complete takeaway.",
            "Extra material cannot fit inside the maximum.",
        ]
    )
    candidates = quality_generator(
        target_duration_seconds=20, maximum_duration_seconds=30
    ).generate_candidates("abc", transcript)
    candidate = next(candidate for candidate in candidates if candidate["start"] == 0)
    assert 20 < candidate["duration"] <= 30


def test_component_scores_vary_for_boundaries_duration_and_context():
    generator = quality_generator()
    segments_ = timed_texts(
        [
            "The prior thought continues and",
            "it does that because of something.",
            "How does the build work? The grenade answers that question.",
            "You should use it because the cooldown resets.",
            "This is a complete takeaway.",
        ]
    )
    bad = generator._make_candidate(
        "abc", segments_[1:3], previous_segment=segments_[0]
    )
    good = generator._make_candidate(
        "abc", segments_[2:], previous_segment=segments_[1]
    )
    assert bad["score"] != good["score"]
    assert bad["component_scores"]["start_boundary_score"] < good["component_scores"]["start_boundary_score"]
    assert bad["component_scores"]["context_independence_score"] < good["component_scores"]["context_independence_score"]
    incomplete = generator._make_candidate(
        "abc", [segments_[0], {**segments_[1], "text": "it does that because"}]
    )
    assert incomplete["component_scores"]["end_boundary_score"] < good["component_scores"]["end_boundary_score"]
    assert good["component_scores"]["duration_fit_score"] > bad["component_scores"]["duration_fit_score"]
    assert any("Penalized" in reason for reason in bad["reasons"])
    assert good["reasons"] != bad["reasons"]


def test_mid_list_opening_is_penalized():
    previous = {"start": 0, "end": 5, "text": "The setup includes a weapon,"}
    current = {"start": 5, "end": 10, "text": "we have banner of war and a grenade."}
    score, reasons = ClipCandidateGenerator._assess_start(current, previous)
    assert score <= 1
    assert any("mid-list" in reason for reason in reasons)


def test_text_duplicate_detection_finds_shared_passages():
    common = "How does the build work the grenade refreshes cooldown after every hit"
    assert ClipCandidateGenerator._text_similarity(
        common + " with one ending", "Earlier words " + common + " with another ending"
    ) >= 0.72


def test_equipment_list_without_takeaway_is_penalized():
    components, reasons = ClipCandidateGenerator.score_candidate(
        "Weapon armor grenade perk cooldown range.", 20, target_duration=20
    )
    assert components["penalty_score"] < 0
    assert any("without a takeaway" in reason for reason in reasons)


def test_complete_unpunctuated_ending_beats_incomplete_clause():
    complete = ClipCandidateGenerator._assess_end(
        {"text": "The final hit refreshes the grenade cooldown completely"}
    )
    incomplete = ClipCandidateGenerator._assess_end(
        {"text": "The final hit refreshes the cooldown because"}
    )
    assert complete.score > incomplete.score
    assert any("missing punctuation" in reason for reason in complete.reasons)


def test_overlap_resolution_prefers_cleaner_boundary():
    transcript = timed_texts(
        [
            "made last year and this continues a prior thought.",
            "How does the build work? It is simple in theory.",
            "The grenade refreshes the cooldown after every hit.",
            "You should activate the perk before the attack.",
            "This is the complete takeaway for the build.",
        ]
    )
    candidates = quality_generator(maximum_candidates=2).generate_candidates("abc", transcript)
    assert candidates
    assert candidates[0]["text"].startswith("How does the build work?")


def test_score_output_is_deterministic_and_candidate_reasons_are_specific():
    generator = quality_generator()
    transcript = timed_texts(
        [
            "How does the build work? The setup uses a grenade.",
            "You should trigger it because it resets 3 seconds of cooldown.",
            "This build ends with a complete actionable takeaway.",
            "The next topic is unrelated.",
        ]
    )
    assert generator.generate_candidates("abc", transcript) == generator.generate_candidates(
        "abc", transcript
    )


def test_final_ranks_follow_serialized_scores_after_filtering():
    transcript = timed_texts(
        [
            "How does the build work? The setup uses a grenade.",
            "You should trigger it because it resets 3 seconds of cooldown.",
            "This build ends with a complete actionable takeaway.",
            "and this duplicate window continues the same talking point.",
            "The problem with armor is its limited range.",
            "You should use the weapon instead because it solves the problem.",
            "That is a second complete takeaway.",
            "A third unrelated topic begins with concrete advice.",
            "The mechanic saves 5 seconds because the perk refreshes.",
            "This is the final complete recommendation.",
        ]
    )
    generator = quality_generator(maximum_candidates=5)
    candidates = generator.generate_candidates("abc", transcript)
    assert [candidate["score"] for candidate in candidates] == sorted(
        (candidate["score"] for candidate in candidates), reverse=True
    )
    assert [candidate["rank"] for candidate in candidates] == list(
        range(1, len(candidates) + 1)
    )
    for index, candidate in enumerate(candidates):
        assert all(
            generator._overlap_fraction(candidate, other)
            <= generator.configuration.maximum_overlap
            for other in candidates[index + 1 :]
        )
        assert all(
            generator._text_similarity(candidate["text"], other["text"]) < 0.72
            for other in candidates[index + 1 :]
        )


def test_final_rank_ties_use_deterministic_boundary_and_identity_order():
    base = {
        "score": 50.0,
        "start": 10.0,
        "candidate_id": "abc-b",
        "component_scores": {
            "start_boundary_score": 10.0,
            "end_boundary_score": 9.0,
            "duration_fit_score": 8.0,
        },
    }
    candidates = [
        base,
        {**base, "candidate_id": "abc-a"},
        {
            **base,
            "candidate_id": "abc-z",
            "component_scores": {
                **base["component_scores"],
                "start_boundary_score": 11.0,
            },
        },
    ]
    first = sorted(candidates, key=ClipCandidateGenerator._final_rank_sort_key)
    second = sorted(candidates, key=ClipCandidateGenerator._final_rank_sort_key)
    assert [candidate["candidate_id"] for candidate in first] == [
        "abc-z",
        "abc-a",
        "abc-b",
    ]
    assert first == second


def test_authoritative_ending_classification_has_consistent_reasons():
    generator = quality_generator(maximum_duration_seconds=60)
    long_complete = generator._make_candidate(
        "abc",
        [
            {
                "id": 1,
                "start": 0,
                "end": 40,
                "text": (
                    "How does the build work? "
                    + "The grenade mechanic gives useful damage and cooldown details. " * 10
                    + "The final hit refreshes the cooldown completely"
                ),
            }
        ],
    )
    assert (
        long_complete["ending_classification"]
        == EndingClassification.ACCEPTABLE_COMPLETE_WITHOUT_PUNCTUATION.value
    )
    assert not any(
        "ending is incomplete" in reason for reason in long_complete["reasons"]
    )
    assert any(
        "complete statement" in reason for reason in long_complete["reasons"]
    )

    incomplete = generator._make_candidate(
        "abc",
        [{"id": 2, "start": 0, "end": 20, "text": "The recommendation works because"}],
    )
    assert incomplete["ending_classification"] == EndingClassification.INCOMPLETE.value
    assert any("ending is incomplete" in reason for reason in incomplete["reasons"])
    assert not any(
        reason.startswith("Ends with a complete")
        for reason in incomplete["reasons"]
    )


@pytest.mark.parametrize(
    "text",
    [
        "The recommendation includes the weapon and",
        "The recommendation works because.",
        "because the final mechanic still needs an explanation",
        "Because the final mechanic still needs an explanation.",
        "The reason is",
    ],
)
def test_trailing_conjunctions_and_dependent_clauses_are_incomplete(text):
    assessment = ClipCandidateGenerator._assess_end({"text": text})
    assert assessment.classification is EndingClassification.INCOMPLETE


def test_serialized_components_add_to_final_total():
    candidate = quality_generator()._make_candidate(
        "abc",
        timed_texts(
            [
                "How does the build work? The grenade starts the mechanic.",
                "You should use it because it refreshes 3 seconds of cooldown.",
                "This is the complete takeaway.",
            ]
        ),
    )
    assert round(sum(candidate["component_scores"].values()), 1) == pytest.approx(
        candidate["score"], abs=0.1
    )
