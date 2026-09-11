"""Regression tests for apps/lyrics/perturb.py (pure, synthetic fixture).

- if lines_of loses words or misgroups lines then broken
- if find_chorus misses the repeated block or invents one then broken
- if a perturbation's source map stops pointing inserted copies at None then broken
- if drop_block keeps the dropped words or the op range lies then broken
"""

from __future__ import annotations

import pytest

from apps.lyrics.perturb import (
    find_chorus,
    lines_of,
    perturb_drop_block,
    perturb_move_block,
    perturb_sheet_extra_chorus,
)

# 8-line synthetic song: chorus = lines 2-3 ("fire fly", "burn high"), repeated at 6-7.
_LINES = [
    ["intro", "one"],          # 0
    ["verse", "line", "two"],  # 1
    ["fire", "fly"],           # 2  chorus
    ["burn", "high"],          # 3  chorus
    ["verse", "three"],        # 4
    ["verse", "four"],         # 5
    ["fire", "fly"],           # 6  chorus repeat
    ["burn", "high"],          # 7  chorus repeat
]
WORDS = [w for line in _LINES for w in line]
FINAL = [i == len(line) - 1 for line in _LINES for i in range(len(line))]


def test_lines_of_roundtrip() -> None:
    lines = lines_of(WORDS, FINAL)
    assert [list(li.words) for li in lines] == _LINES, "if line grouping drifts then broken"
    assert lines[2].start_idx == 5 and lines[2].end_idx == 6, "if word ranges drift then broken"
    assert sum(len(li.words) for li in lines) == len(WORDS), "if words are lost then broken"


def test_find_chorus_finds_the_repeat() -> None:
    assert find_chorus(lines_of(WORDS, FINAL)) == (2, 3), "if chorus != lines 2-3 then broken"
    no_repeat = lines_of(["a", "b", "c", "d"], [True, True, True, True])
    assert find_chorus(no_repeat) is None, "if a chorus is invented then broken"


def test_drop_block_removes_exactly_the_op_range() -> None:
    sheet = perturb_drop_block(WORDS, FINAL, seed=7)
    (op,) = sheet.ops
    assert op.op == "drop_block"
    dropped = set(range(op.word_start, op.word_end + 1))
    assert sheet.source_idx == [i for i in range(len(WORDS)) if i not in dropped], (
        "if the source map disagrees with the op's dropped range then broken"
    )
    assert len(sheet.words) == len(WORDS) - len(dropped), "if word count lies then broken"


def test_sheet_extra_chorus_inserts_none_sourced_copy() -> None:
    sheet = perturb_sheet_extra_chorus(WORDS, FINAL)
    (op,) = sheet.ops
    assert op.op == "insert_repeat" and (op.line_start, op.line_end) == (2, 3)
    assert len(sheet.words) == len(WORDS) + 4, "if the chorus copy is not 4 words then broken"
    inserted = [i for i, s in enumerate(sheet.source_idx) if s is None]
    assert len(inserted) == 4, "if inserted words are not exactly the copied chorus then broken"
    assert [sheet.words[i] for i in inserted] == ["fire", "fly", "burn", "high"]
    surviving = [s for s in sheet.source_idx if s is not None]
    assert surviving == sorted(surviving), "if surviving words fall out of order then broken"


def test_move_block_preserves_multiset() -> None:
    sheet = perturb_move_block(WORDS, FINAL, seed=3)
    assert sorted(sheet.words) == sorted(WORDS), "if move loses or invents words then broken"
    assert sheet.words != WORDS, "if move is a no-op then broken"
    assert sorted(s for s in sheet.source_idx) == list(range(len(WORDS))), (
        "if the source map is not a permutation for a pure move then broken"
    )


def test_short_song_fails_fast() -> None:
    with pytest.raises(ValueError, match="too short"):
        perturb_drop_block(["a", "b"], [True, True], seed=1)


def test_audio_extra_chorus_drops_second_occurrence() -> None:
    from apps.lyrics.perturb import perturb_audio_extra_chorus

    sheet = perturb_audio_extra_chorus(WORDS, FINAL)
    (op,) = sheet.ops
    assert op.op == "drop_repeat" and (op.line_start, op.line_end) == (6, 7), (
        "if the SECOND chorus occurrence (lines 6-7) is not the one dropped then broken"
    )
    assert len(sheet.words) == len(WORDS) - 4, "if the dropped repeat is not 4 words then broken"
    assert "fire" in sheet.words, "if the FIRST occurrence vanished too then broken"
