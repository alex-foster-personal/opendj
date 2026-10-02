"""Drop phantom tail words from aligner output before karaoke_words are written.

ASR hallucinations after the last sung line (repeated "thank you", words past
track duration) are removed here so the waveform word lane and API serve honest
timings only. Does not alter ``source`` or witness on kept words.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

TAIL_MARGIN_S: float = 0.25

_WEAK_WITNESS: frozenset[str] = frozenset({"contradict", "lost", "unheard"})

# Spirit of scripts/lyrics_oltf_spike.py LOOP_* without importing the script.
_LOOP_MIN_WORDS: int = 20
_LOOP_TOP_SHARE: float = 0.5

_THANK_RE = re.compile(r"^thanks?$|^thank\s+you$", re.IGNORECASE)


@dataclass(frozen=True)
class TailSanitizeReport:
    dropped: tuple[tuple[int, str, str], ...]


def _normalize_text(text: str) -> str:
    cleaned = text.strip().lower().strip(".,!?\"'")
    return re.sub(r"\s+", " ", cleaned)


def _is_thank_you_token(text: str) -> bool:
    norm = _normalize_text(text)
    if not norm:
        return False
    if norm in ("thank", "thanks", "you"):
        return True
    return bool(_THANK_RE.match(norm))


def _tail_is_loop_garbage(tail: Sequence[Mapping[str, Any]]) -> bool:
    if len(tail) < _LOOP_MIN_WORDS:
        return False
    counts: dict[str, int] = {}
    for entry in tail:
        key = _normalize_text(str(entry.get("word", "")))
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return False
    top = max(counts, key=counts.__getitem__)
    return counts[top] / len(tail) > _LOOP_TOP_SHARE


def _trailing_hallucination_suffix_len(tail: Sequence[Mapping[str, Any]]) -> int:
    if not tail:
        return 0
    if _tail_is_loop_garbage(tail):
        return len(tail)
    n = 0
    for entry in reversed(tail):
        witness = entry.get("witness")
        if witness in _WEAK_WITNESS and _is_thank_you_token(str(entry.get("word", ""))):
            n += 1
        else:
            break
    return n


def _validate_duration(duration_s: float | None) -> float | None:
    """Return the duration cutoff, or raise on a malformed non-None value."""
    if duration_s is None:
        return None
    if not isinstance(duration_s, (int, float)) or duration_s <= 0:
        raise ValueError(f"duration_s must be a positive number or None, got {duration_s!r}")
    return float(duration_s) - TAIL_MARGIN_S


def _is_past_duration(entry: Mapping[str, Any], duration_cutoff: float | None) -> bool:
    if duration_cutoff is None:
        return False
    start = entry.get("start_s")
    return isinstance(start, (int, float)) and float(start) > duration_cutoff


def _drop_past_duration(
    words: Sequence[Mapping[str, Any]],
    duration_cutoff: float | None,
) -> tuple[list[Mapping[str, Any]], list[int], list[tuple[int, str, str]]]:
    """Split ``words`` into what survives the duration cutoff and what dropped."""
    dropped: list[tuple[int, str, str]] = []
    kept: list[Mapping[str, Any]] = []
    kept_orig_idx: list[int] = []
    for index, entry in enumerate(words):
        if _is_past_duration(entry, duration_cutoff):
            dropped.append((index, str(entry.get("word", "")), "past_duration"))
            continue
        kept.append(entry)
        kept_orig_idx.append(index)
    return kept, kept_orig_idx, dropped


def _last_line_final_index(kept: Sequence[Mapping[str, Any]]) -> int:
    """Return the index of the last word marked ``line_final``, or -1."""
    last_final = -1
    for i, entry in enumerate(kept):
        if entry.get("line_final") is True:
            last_final = i
    return last_final


def sanitize_words_for_artifact(
    words: Sequence[Mapping[str, Any]],
    *,
    duration_s: float | None,
) -> tuple[list[Mapping[str, Any]], TailSanitizeReport]:
    """Return words safe to pass to ``karaoke_cache.build_words``."""
    duration_cutoff = _validate_duration(duration_s)
    kept, kept_orig_idx, dropped = _drop_past_duration(words, duration_cutoff)

    last_final = _last_line_final_index(kept)
    if last_final < 0 or last_final >= len(kept) - 1:
        return kept, TailSanitizeReport(dropped=tuple(dropped))

    head = kept[: last_final + 1]
    tail = kept[last_final + 1 :]
    tail_orig = kept_orig_idx[last_final + 1 :]
    trim = _trailing_hallucination_suffix_len(tail)
    if trim:
        for j in range(len(tail) - trim, len(tail)):
            entry = tail[j]
            dropped.append((tail_orig[j], str(entry.get("word", "")), "post_final_hallucination"))
        kept = head + tail[: len(tail) - trim]

    return kept, TailSanitizeReport(dropped=tuple(dropped))


__all__ = ["TAIL_MARGIN_S", "TailSanitizeReport", "sanitize_words_for_artifact"]
