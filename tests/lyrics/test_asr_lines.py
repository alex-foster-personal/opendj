"""ASR word grouping for line-level lyrics (LYRICS-07)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.lyrics.asr_lines import AsrWord, group_asr_words_into_lines, parse_asr_words

pytestmark = pytest.mark.requirement("LYRICS-07")

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "lyrics_asr" / "sample.json"


def test_fixture_groups_pause_gap_into_two_lines() -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    words = parse_asr_words(payload["words"])
    lines = group_asr_words_into_lines(words)
    assert [(line.start_ms, line.text) for line in lines] == [
        (500, "hello world"),
        (3000, "again"),
    ]


def test_punctuation_break_starts_new_line() -> None:
    words = (
        AsrWord("Hello.", 0.0, 0.4),
        AsrWord("World", 0.5, 0.9),
    )
    lines = group_asr_words_into_lines(words)
    assert [line.text for line in lines] == ["Hello.", "World"]


def test_twelve_word_cap_starts_new_line() -> None:
    words = tuple(
        AsrWord(f"w{i}", float(i), float(i) + 0.1) for i in range(14)
    )
    lines = group_asr_words_into_lines(words)
    assert len(lines[0].text.split()) == 13
    assert lines[1].text.split() == ["w13"]


def test_out_of_order_words_fail_loud() -> None:
    with pytest.raises(ValueError, match="out of order"):
        parse_asr_words(
            [
                {"word": "one", "start_s": 2.0, "end_s": 2.5},
                {"word": "two", "start_s": 1.0, "end_s": 1.5},
            ]
        )
