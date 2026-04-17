"""Unit tests for apps.voice.grammar (VOICE-01)."""
from __future__ import annotations

import pytest

from apps.voice import grammar


pytestmark = pytest.mark.requirement("VOICE-01")


@pytest.mark.parametrize(
    "transcript,expected_kind,expected_slots",
    [
        ("find daft punk", "SEARCH", {"query": "daft punk"}),
        ("Find The Prodigy", "SEARCH", {"query": "the prodigy"}),
        ("search for techno", "SEARCH", {"query": "techno"}),
        ("what's this bpm", "READ_BPM", {}),
        ("what is the tempo?", "READ_BPM", {}),
        ("bpm", "READ_BPM", {}),
        ("tempo", "READ_BPM", {}),
        ("what's the key?", "READ_KEY", {}),
        ("key of this track", "READ_KEY", {}),
        ("key", "READ_KEY", {}),
        ("play the next track", "ADVANCE_QUEUE", {}),
        ("next track", "ADVANCE_QUEUE", {}),
        ("next up", "ADVANCE_QUEUE", {}),
        ("mute voice", "MUTE_VOICE", {}),
        ("mute voice commands", "MUTE_VOICE", {}),
        ("unmute voice", "UNMUTE_VOICE", {}),
        ("un-mute voice commands", "UNMUTE_VOICE", {}),
        ("save that last transition as cue points", "SAVE_CUE", {}),
        ("save transition", "SAVE_CUE", {}),
        ("bookmark this transition", "SAVE_CUE", {}),
        ("rate this 5 stars", "RATE_TRACK", {"stars": 5}),
        ("rate 4 stars", "RATE_TRACK", {"stars": 4}),
        ("rate this five stars", "RATE_TRACK", {"stars": 5}),
    ],
)
def test_positive_matches(transcript, expected_kind, expected_slots):
    intent = grammar.parse(transcript)
    assert intent is not None, f"{transcript!r} should match"
    assert intent.kind == expected_kind
    for key, value in expected_slots.items():
        assert intent.slots.get(key) == value


@pytest.mark.parametrize(
    "transcript",
    [
        "",
        "   ",
        "this is nonsense",
        "hello world",
        "what",
        "random drumbeat here",
    ],
)
def test_negative_matches(transcript):
    assert grammar.parse(transcript) is None


class TestNormalisation:
    def test_number_word_map_works(self):
        intent = grammar.parse("rate this two stars")
        assert intent is not None
        assert intent.slots["stars"] == 2

    def test_case_insensitive(self):
        intent = grammar.parse("FIND Techno")
        assert intent is not None
        assert intent.slots["query"] == "techno"

    def test_whitespace_collapse(self):
        intent = grammar.parse("   find   daft   punk  ")
        assert intent is not None
        assert intent.slots["query"] == "daft punk"


def test_parse_none_input():
    assert grammar.parse(None) is None  # type: ignore[arg-type]


def test_raw_transcript_preserved():
    intent = grammar.parse("find X")
    assert intent is not None
    assert intent.raw_transcript == "find X"
