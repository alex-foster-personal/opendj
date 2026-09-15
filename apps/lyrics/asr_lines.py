"""Canonical ASR word timestamps to line-level lyric grouping (LYRICS-07)."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from apps.lyrics.cache import LyricLine

_PAUSE_GAP_S = 1.2
_MAX_WORDS_PER_LINE = 12
_SENTENCE_END = re.compile(r"[.!?]$")


@dataclass(frozen=True)
class AsrWord:
    word: str
    start_s: float
    end_s: float


def parse_asr_words(words: list[object]) -> tuple[AsrWord, ...]:
    """Validate ASR ``words`` entries and return them in time order."""
    if not isinstance(words, list):
        raise ValueError("ASR transcript words must be a list")
    parsed: list[AsrWord] = []
    previous_start = -math.inf
    for index, entry in enumerate(words):
        if not isinstance(entry, dict):
            raise ValueError(f"ASR word {index} must be an object")
        word = entry.get("word")
        start_s = entry.get("start_s")
        end_s = entry.get("end_s")
        if not isinstance(word, str) or not word.strip():
            raise ValueError(f"ASR word {index} has invalid word text")
        if not _finite_non_negative(start_s) or not _finite_non_negative(end_s):
            raise ValueError(f"ASR word {index} has invalid timestamps")
        if float(start_s) > float(end_s):
            raise ValueError(f"ASR word {index} starts after it ends")
        start = float(start_s)
        if start < previous_start:
            raise ValueError(f"ASR words are out of order at index {index}")
        previous_start = start
        parsed.append(AsrWord(word=word.strip(), start_s=start, end_s=float(end_s)))
    return tuple(parsed)


def group_asr_words_into_lines(words: tuple[AsrWord, ...]) -> tuple[LyricLine, ...]:
    """Group ASR words into line-synced lyric rows."""
    if not words:
        return ()
    lines: list[tuple[AsrWord, ...]] = []
    current: list[AsrWord] = [words[0]]
    for word in words[1:]:
        if _starts_new_line(current[-1], word, len(current)):
            lines.append(tuple(current))
            current = [word]
        else:
            current.append(word)
    lines.append(tuple(current))
    return tuple(
        LyricLine(start_ms=round(line[0].start_s * 1000), text=" ".join(w.word for w in line))
        for line in lines
    )


def _starts_new_line(previous: AsrWord, current: AsrWord, words_in_line: int) -> bool:
    if current.start_s - previous.end_s > _PAUSE_GAP_S:
        return True
    if words_in_line > _MAX_WORDS_PER_LINE:
        return True
    if _SENTENCE_END.search(previous.word):
        return True
    return False


def _finite_non_negative(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    number = float(value)
    return math.isfinite(number) and number >= 0.0


__all__ = ["AsrWord", "group_asr_words_into_lines", "parse_asr_words"]
