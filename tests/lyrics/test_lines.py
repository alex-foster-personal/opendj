"""Line derivation - the canonical grouping every lyric surface renders.

Regression lines (one-line if/then, house format):
- if words stop grouping on line_final (or trailing words are dropped) then broken
- if line quality counts unjudged words in its denominator then broken
- if a paragraph break is invented where there is no timing evidence then broken
- if the quality band thresholds drift from the module constants then broken
- if derive_lines starts demanding a concrete storage row type then broken

The word here is a local record satisfying ``lines.WordRow``, not a storage
class: grouping is deliberately decoupled from whichever reader produced the
words (CloudSync landing D13.2 retires the lyric_word table for a per-track
artifact).
"""

from __future__ import annotations

from dataclasses import dataclass

from apps.lyrics.lines import PARAGRAPH_GAP_S, WordRow, derive_lines


@dataclass(frozen=True)
class _Word:
    idx: int
    word: str
    start_s: float | None
    end_s: float | None
    witness: str | None
    line_final: bool


def _w(idx: int, word: str, start: float | None, end: float | None,
       witness: str | None, final: bool = False) -> _Word:
    return _Word(idx=idx, word=word, start_s=start, end_s=end,
                 witness=witness, line_final=final)


def test_any_word_row_shape_is_accepted():
    """If derive_lines couples to a storage class then artifact readers break."""
    assert isinstance(_w(0, "a", 1.0, 1.1, "agree"), WordRow)


def test_groups_on_line_final_and_keeps_trailing_words():
    words = [
        _w(0, "your", 1.0, 1.2, "agree"),
        _w(1, "personal", 1.3, 1.8, "contradict", final=True),
        _w(2, "jesus", 5.0, 5.5, "agree"),  # no closing flag: still a line
    ]
    lines = derive_lines(words)
    assert [ln.text for ln in lines] == ["your personal", "jesus"]
    assert (lines[0].first_idx, lines[0].last_idx) == (0, 1)
    assert lines[0].start_s == 1.0 and lines[0].end_s == 1.8


def test_quality_uses_judged_words_only_and_bands_follow_constants():
    lines = derive_lines([
        _w(0, "a", 1.0, 1.1, "agree"),
        _w(1, "b", 1.2, 1.3, "contradict"),
        _w(2, "c", 1.4, 1.5, None, final=True),  # unjudged: out of denominator
        _w(3, "d", 2.0, 2.1, None, final=True),  # wholly unjudged line
        _w(4, "e", 3.0, 3.1, "lost"),
        _w(5, "f", 3.2, 3.3, "contradict", final=True),
    ])
    assert lines[0].quality == 0.5 and lines[0].band == "uncertain"
    assert lines[0].n_judged == 2 and lines[0].n_red == 1
    assert lines[1].quality is None and lines[1].band == "unjudged"
    assert lines[2].quality == 0.0 and lines[2].band == "bad"


def test_paragraphs_come_from_gaps_never_from_guesswork():
    gap = PARAGRAPH_GAP_S + 0.1
    lines = derive_lines([
        _w(0, "verse", 1.0, 1.5, "agree", final=True),
        _w(1, "chorus", 1.5 + gap, 1.9 + gap, "agree", final=True),
        _w(2, "untimed", None, None, "agree", final=True),
        _w(3, "coda", 50.0, 50.5, "agree", final=True),
    ])
    assert lines[0].para_final is True, "a real gap ends a paragraph"
    assert lines[1].para_final is False, "no timing on the NEXT line = no evidence"
    assert lines[2].para_final is False, "an untimed line cannot claim a gap"
    assert lines[3].para_final is True, "the last line always closes one"


def test_close_lines_do_not_break_paragraphs():
    lines = derive_lines([
        _w(0, "one", 1.0, 1.5, "agree", final=True),
        _w(1, "two", 1.8, 2.2, "agree", final=True),
    ])
    assert lines[0].para_final is False
