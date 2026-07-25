import pytest

from backend.services.clip_context_expander import (
    ClipContextExpander, ContextExpansionConfiguration,
)


def transcript(*segments):
    return {
        "segments": [
            {"start": start, "end": end, "text": text}
            for start, end, text in segments
        ]
    }


def expand(start=40, end=60, source=150, data=None, config=None):
    return ClipContextExpander().expand(
        start, end, source, data or {"segments": []},
        config or ContextExpansionConfiguration.for_profile("reaction"),
    )


def test_reaction_and_compact_defaults():
    reaction = ContextExpansionConfiguration.for_profile("reaction")
    assert (reaction.preferred_lead_in, reaction.preferred_tail) == (15, 12)
    assert (reaction.minimum_lead_in, reaction.minimum_tail) == (10, 8)
    assert (
        reaction.minimum_final_duration, reaction.target_final_duration,
        reaction.maximum_final_duration,
    ) == (50, 60, 90)
    compact = ContextExpansionConfiguration.for_profile("compact")
    assert (compact.preferred_lead_in, compact.preferred_tail) == (6, 6)
    assert (
        compact.minimum_final_duration, compact.target_final_duration,
        compact.maximum_final_duration,
    ) == (35, 45, 60)


def test_candidate_is_contained_and_preferred_context_is_present():
    result = expand(start=50, end=80)
    assert result.render_start <= 50 < 80 <= result.render_end
    assert result.lead_in_seconds >= 15
    assert result.tail_seconds >= 12
    assert result.render_duration == 60


def test_clamps_at_source_edges_and_handles_short_source():
    beginning = expand(start=3, end=18, source=100)
    assert beginning.render_start == 0
    ending = expand(start=70, end=79, source=80)
    assert ending.render_end == 80
    short = expand(start=2, end=8, source=10)
    assert (short.render_start, short.render_end, short.render_duration) == (0, 10, 10)


def test_candidate_longer_than_maximum_requires_override():
    with pytest.raises(ValueError, match="allow-longer"):
        expand(start=10, end=101, source=120)
    config = ContextExpansionConfiguration.for_profile("reaction", allow_longer=True)
    result = expand(start=10, end=101, source=120, config=config)
    assert result.render_duration >= 91


def test_maximum_reduces_only_optional_context():
    config = ContextExpansionConfiguration.for_profile(
        "compact", minimum_final_duration=30, target_final_duration=40,
        maximum_final_duration=40,
    )
    result = expand(start=40, end=70, config=config)
    assert result.render_duration == 40
    assert result.render_start <= 40 and result.render_end >= 70


def test_non_speech_lead_in_is_kept():
    result = expand(data=transcript((45, 55, "spoken anchor.")))
    assert result.render_start < 40


def test_sentence_and_pause_boundary_snapping():
    data = transcript(
        (18, 24, "Previous sentence."),
        (26, 33, "Here is the setup."),
        (40, 50, "Anchor."),
    )
    config = ContextExpansionConfiguration.for_profile(
        "reaction", minimum_final_duration=36, target_final_duration=36,
    )
    result = expand(start=40, end=50, data=data, config=config)
    assert result.render_start == 26
    assert result.start_boundary_method in {"meaningful_pause", "sentence_boundary"}


@pytest.mark.parametrize("anchor,payoff", [
    ("What rank do you think?", "The answer is diamond."),
    ("Guess the rank.", "He ranked gold!"),
    ("Watch this.", "Wow!"),
    ("My guess is silver.", "The result is platinum."),
])
def test_payoff_language_continues_to_result(anchor, payoff):
    data = transcript(
        (35, 48, anchor), (50, 63, "We keep watching."),
        (64, 69, payoff), (72, 77, "Anyway next topic."),
    )
    result = expand(start=35, end=48, source=100, data=data)
    assert result.render_end >= 69
    assert any("payoff" in reason.lower() for reason in result.expansion_reasons)


def test_new_topic_transition_and_complete_sentence_stop():
    data = transcript(
        (40, 50, "Let's see what happens?"),
        (55, 63, "The answer is ten!"),
        (66, 72, "Anyway next topic."),
    )
    result = expand(start=40, end=50, source=120, data=data)
    assert result.render_end <= 72


def test_unfinished_unit_is_extended():
    config = ContextExpansionConfiguration.for_profile(
        "compact", minimum_final_duration=20, target_final_duration=20,
        maximum_final_duration=60,
    )
    data = transcript((30, 40, "Anchor."), (44, 50, "This thought continues"))
    result = expand(start=30, end=40, source=100, data=data, config=config)
    assert result.render_end >= 50
    assert result.end_boundary_method == "unfinished_sentence_extended"


def test_deterministic_and_reasons_are_human_readable():
    first = expand()
    second = expand()
    assert first == second
    assert first.expansion_reasons
    assert all(reason.endswith(".") for reason in first.expansion_reasons)


@pytest.mark.parametrize("overrides", [
    {"minimum_final_duration": 61, "target_final_duration": 60},
    {"target_final_duration": 91, "maximum_final_duration": 90},
    {"preferred_lead_in": -1},
])
def test_invalid_configuration_relationships(overrides):
    with pytest.raises(ValueError):
        ContextExpansionConfiguration.for_profile("reaction", **overrides)
