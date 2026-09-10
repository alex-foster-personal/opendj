"""Perturbation harness: fabricate wrong-version lyric sheets with KNOWN edits.

Track A of specs/lyrics-version-check.md round 1. Each perturbation takes a
song's true word list (with line_final flags), applies ONE structural edit at
line granularity, and returns the corrupted sheet plus the ground-truth edit
op and a word-level source map (perturbed index -> original word index, None
for inserted words). The map is what lets round 2 score post-repair timestamps
on surviving words only, with an honest denominator.

v1 taxonomy is STRUCTURAL only (drop_block, sheet_extra_chorus, sheet_prepend_hook,
move_block; wrong_song lives in the runner since it needs a second song).
Word-level edits (censor/swap) are spec'd for a later round.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from apps.lyrics.asr_match import normalize_word

#-----------------------------------------------------------------------------


@dataclass(frozen=True)
class Line:
    words: tuple[str, ...]
    start_idx: int  # index of first word in the song's word list
    end_idx: int  # inclusive


@dataclass(frozen=True)
class EditOp:
    op: str  # drop_block | insert_repeat | move_block | wrong_song
    line_start: int  # in ORIGINAL line coordinates
    line_end: int  # inclusive
    word_start: int  # in ORIGINAL word coordinates
    word_end: int  # inclusive
    note: str


@dataclass(frozen=True)
class PerturbedSheet:
    words: list[str]
    source_idx: list[int | None]  # per perturbed word: original index, None = inserted copy
    ops: list[EditOp]


def lines_of(words: list[str], line_final: list[bool]) -> list[Line]:
    if len(words) != len(line_final):
        raise ValueError("words and line_final must be the same length")
    if not words:
        raise ValueError("no words")
    lines: list[Line] = []
    start = 0
    for i, final in enumerate(line_final):
        if final:
            lines.append(Line(tuple(words[start : i + 1]), start, i))
            start = i + 1
    if start != len(words):  # trailing words with no line_final close the last line
        lines.append(Line(tuple(words[start:]), start, len(words) - 1))
    return lines


def find_chorus(lines: list[Line]) -> tuple[int, int] | None:
    """Longest contiguous line block (2..8 lines) that repeats verbatim
    (normalized) elsewhere in the song; returns (line_start, line_end) of its
    first occurrence, or None if nothing repeats."""
    norm = [" ".join(normalize_word(w) for w in line.words) for line in lines]
    for length in range(min(8, len(lines) // 2), 1, -1):
        for start in range(len(lines) - 2 * length + 1):
            block = norm[start : start + length]
            for other in range(start + length, len(lines) - length + 1):
                if norm[other : other + length] == block:
                    return (start, start + length - 1)
    return None


#-----------------------------------------------------------------------------


def _sheet_from_line_seq(
    lines: list[Line], line_seq: list[int], ops: list[EditOp]
) -> PerturbedSheet:
    """Assemble a sheet from a sequence of original line indices. A line index
    appearing a second+ time contributes source_idx=None words (inserted copy,
    no original timestamp of its own)."""
    words: list[str] = []
    source_idx: list[int | None] = []
    seen: set[int] = set()
    for li in line_seq:
        line = lines[li]
        first_use = li not in seen
        seen.add(li)
        for k, w in enumerate(line.words):
            words.append(w)
            source_idx.append(line.start_idx + k if first_use else None)
    return PerturbedSheet(words=words, source_idx=source_idx, ops=ops)


def _block_op(op: str, lines: list[Line], line_start: int, line_end: int, note: str) -> EditOp:
    return EditOp(
        op=op, line_start=line_start, line_end=line_end,
        word_start=lines[line_start].start_idx, word_end=lines[line_end].end_idx,
        note=note,
    )


def perturb_drop_block(words: list[str], line_final: list[bool], seed: int) -> PerturbedSheet:
    """Drop a contiguous ~fifth of the song's lines (a verse-sized hole)."""
    lines = lines_of(words, line_final)
    if len(lines) < 6:
        raise ValueError(f"song too short to drop a block ({len(lines)} lines)")
    rng = random.Random(seed)
    block_len = max(3, len(lines) // 5)
    start = rng.randrange(0, len(lines) - block_len + 1)
    end = start + block_len - 1
    seq = [i for i in range(len(lines)) if not (start <= i <= end)]
    op = _block_op("drop_block", lines, start, end, f"dropped {block_len} lines")
    return _sheet_from_line_seq(lines, seq, [op])


def perturb_sheet_extra_chorus(words: list[str], line_final: list[bool]) -> PerturbedSheet:
    """Duplicate the detected chorus immediately after its first occurrence."""
    lines = lines_of(words, line_final)
    chorus = find_chorus(lines)
    if chorus is None:
        raise ValueError("no repeated line block found -- sheet_extra_chorus not applicable")
    start, end = chorus
    seq = list(range(end + 1)) + list(range(start, len(lines)))
    op = _block_op("insert_repeat", lines, start, end, "chorus doubled after first occurrence")
    return _sheet_from_line_seq(lines, seq, [op])


def perturb_sheet_prepend_hook(words: list[str], line_final: list[bool]) -> PerturbedSheet:
    """Copy the detected chorus to the very start (extended-mix intro hook)."""
    lines = lines_of(words, line_final)
    chorus = find_chorus(lines)
    if chorus is None:
        raise ValueError("no repeated line block found -- sheet_prepend_hook not applicable")
    start, end = chorus
    seq = list(range(start, end + 1)) + list(range(len(lines)))
    op = _block_op("insert_repeat", lines, start, end, "chorus copied to song start")
    sheet = _sheet_from_line_seq(lines, seq, [op])
    # _sheet_from_line_seq gives FIRST use the source identity, but here the
    # first use is the PREPENDED (unsung) copy -- true timestamps belong to the
    # body occurrence. Swap: prepended copy -> None, body occurrence -> identity.
    n_copy = lines[end].end_idx - lines[start].start_idx + 1
    body_positions = [i for i, s in enumerate(sheet.source_idx) if i >= n_copy and s is None]
    fixed = list(sheet.source_idx)
    for head_pos, body_pos in zip(range(n_copy), body_positions[:n_copy], strict=True):
        fixed[body_pos] = fixed[head_pos]
        fixed[head_pos] = None
    return PerturbedSheet(words=sheet.words, source_idx=fixed, ops=sheet.ops)


def perturb_audio_extra_chorus(words: list[str], line_final: list[bool]) -> PerturbedSheet:
    """Drop the chorus's SECOND occurrence from the sheet: the audio now sings
    more repeats than the sheet lists -- the radio-sheet-on-extended-mix case
    (directional mirror of perturb_sheet_extra_chorus)."""
    lines = lines_of(words, line_final)
    chorus = find_chorus(lines)
    if chorus is None:
        raise ValueError("no repeated line block found -- audio_extra_chorus not applicable")
    start, end = chorus
    length = end - start + 1
    norm = [" ".join(normalize_word(w) for w in line.words) for line in lines]
    block = norm[start : end + 1]
    second = next(
        (i for i in range(end + 1, len(lines) - length + 1) if norm[i : i + length] == block),
        None,
    )
    if second is None:
        raise AssertionError("find_chorus guarantees a second occurrence; none found")
    seq = [i for i in range(len(lines)) if not (second <= i <= second + length - 1)]
    op = _block_op("drop_repeat", lines, second, second + length - 1,
                   "second chorus occurrence removed from the sheet (audio sings it)")
    return _sheet_from_line_seq(lines, seq, [op])


def perturb_move_block(words: list[str], line_final: list[bool], seed: int) -> PerturbedSheet:
    """Remove a verse-sized block and reinsert it at a different line position."""
    lines = lines_of(words, line_final)
    if len(lines) < 8:
        raise ValueError(f"song too short to move a block ({len(lines)} lines)")
    rng = random.Random(seed)
    block_len = max(3, len(lines) // 5)
    start = rng.randrange(0, len(lines) - block_len + 1)
    end = start + block_len - 1
    rest = [i for i in range(len(lines)) if not (start <= i <= end)]
    positions = [p for p in range(len(rest) + 1) if abs(p - start) >= 2]
    insert_at = rng.choice(positions)
    seq = rest[:insert_at] + list(range(start, end + 1)) + rest[insert_at:]
    op = _block_op(
        "move_block", lines, start, end,
        f"moved {block_len} lines to line pos {insert_at}",
    )
    return _sheet_from_line_seq(lines, seq, [op])
