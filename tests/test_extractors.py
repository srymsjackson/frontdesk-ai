"""Deterministic tests for the regex fallback + cleaning layer.

These never touch the network. They pin down the behaviour the booking flow
falls back to whenever the LLM is unavailable or returns something unusable,
which is exactly the safety net the product's value depends on.
"""

import pytest

from app.services import ai_service as ai


# --- name extraction -------------------------------------------------------

@pytest.mark.conversation
@pytest.mark.parametrize(
    "utterance,expected",
    [
        ("my name is Marcus", "Marcus"),
        ("my name's Dede", "Dede"),
        ("hi this is Priya calling", "Priya"),
        ("MY NAME IS jordan", "Jordan"),        # case-insensitive + title-cased
    ],
)
def test_fallback_extract_name_hits(utterance, expected):
    assert ai.fallback_extract_name(utterance) == expected


@pytest.mark.conversation
@pytest.mark.parametrize(
    "utterance,expected",
    [
        ("hey I'm Marcus", "Marcus"),
        ("i'm priya, need a cut", "Priya"),
    ],
)
def test_fallback_extract_name_im_pattern(utterance, expected):
    # "I'm X" is a very common self-introduction the original pattern missed.
    assert ai.fallback_extract_name(utterance) == expected


@pytest.mark.conversation
@pytest.mark.parametrize(
    "utterance",
    [
        "yeah I want a fade",              # no name present
        "uhh sometime thursday maybe",
        "",                                 # empty / unintelligible turn
    ],
)
def test_fallback_extract_name_no_false_positive(utterance):
    # When no name was actually stated, we must NOT invent one.
    assert ai.fallback_extract_name(utterance) is None


@pytest.mark.hostile
@pytest.mark.parametrize(
    "utterance",
    [
        "are you a robot? this is stupid",   # the bug this suite first caught
        "this is ridiculous",
        "i'm annoyed with this",
        "this is so pointless",
    ],
)
def test_fallback_extract_name_rejects_non_names(utterance):
    # Hostile "this is <adjective>" / "I'm <adjective>" must not become a name.
    assert ai.fallback_extract_name(utterance) is None


# --- service extraction ----------------------------------------------------

@pytest.mark.ambiguous
@pytest.mark.parametrize(
    "utterance,expected",
    [
        ("I'd love a fade", "fade"),
        ("just a beard trim please", "beard trim"),
        ("can I get a haircut", "haircut"),
        ("need a cut", "haircut"),          # 'cut' maps to haircut
    ],
)
def test_fallback_extract_service(utterance, expected):
    assert ai.fallback_extract_service(utterance) == expected


@pytest.mark.ambiguous
def test_service_none_when_absent():
    assert ai.fallback_extract_service("is anyone there") is None


# --- time extraction -------------------------------------------------------

@pytest.mark.ambiguous
@pytest.mark.parametrize(
    "utterance,expected_contains",
    [
        ("around 3pm", "3pm"),
        ("can you do 2:30 pm", "2:30 pm"),
        ("tomorrow morning works", "tomorrow"),
        ("how about 11 a.m.", "11 am"),     # punctuation stripped
    ],
)
def test_fallback_extract_time(utterance, expected_contains):
    got = ai.fallback_extract_time(utterance)
    assert got is not None
    assert expected_contains in got


@pytest.mark.ambiguous
def test_time_none_when_vague():
    # "sometime next week" has no clock time or 'tomorrow' anchor — the LLM
    # handles these; the fallback correctly declines rather than guessing.
    assert ai.fallback_extract_time("sometime next week maybe") is None


# --- cleaners --------------------------------------------------------------

@pytest.mark.conversation
def test_clean_name_titlecases():
    assert ai.clean_name("  marCUS  ") == "Marcus"


@pytest.mark.conversation
def test_clean_time_strips_periods_and_lowers():
    assert ai.clean_time("Thursday at 2:00 P.M.") == "thursday at 2:00 pm"


@pytest.mark.ambiguous
@pytest.mark.parametrize(
    "value,expected",
    [
        ("any barber is fine", "no preference"),
        ("whoever", "no preference"),
        ("doesn't matter", "no preference"),
        ("I want Tony", "I want Tony"),     # a real preference passes through
    ],
)
def test_clean_barber(value, expected):
    assert ai.clean_barber(value) == expected


@pytest.mark.conversation
def test_none_inputs_stay_none():
    assert ai.clean_name(None) is None
    assert ai.clean_time(None) is None
    assert ai.clean_service(None) is None
    assert ai.clean_barber(None) is None
