"""Drop phantom tail words from aligner output before karaoke_words are written.

Two narrow rules, each requiring evidence that the words are not sung audio:

1. ``past_duration``: a word whose ``start_s`` is at or beyond the track's
   actual duration has no audio under it at all, so it is always dropped. The
   valid interval is never shortened: a word starting at 99.9 s of a 100 s
   track is kept.
2. ``post_final_hallucination``: the known Whisper silence hallucination (one
   or more "thank you" / "thanks for watching" style phrases) is dropped only
   when ALL of these hold for the trailing span:
   - its words, joined, are exactly one or more phrases from
     ``HALLUCINATION_PHRASES`` (no other word rides along);
   - it starts after the last ``line_final`` word and holds no ``line_final``
     word itself (it is not part of a sung line);
   - every word in it carries a weak witness (``contradict`` / ``lost`` /
     ``unheard``), i.e. the ASR cross-check does not hear it;
   - it begins at least ``HALLUCINATION_GAP_S`` after the previous word ends,
     i.e. it sits in silence after the vocals stopped.
   A missing timestamp on either side of the gap means the gap cannot be
   measured, and the span is kept. Repetition alone is never evidence: a
   repeated chorus or outro is kept whatever its witness.

Does not alter ``source`` or witness on kept words.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: Minimum silence, in seconds, between the previous word's end and the start
#: of a trailing hallucination-phrase span before that span may be dropped.
HALLUCINATION_GAP_S: float = 2.0

#: Phrases Whisper emits over trailing silence (normalized, space-joined).
HALLUCINATION_PHRASES: tuple[str, ...] = (
    "thank you",
    "thank you so much",
    "thank you for watching",
    "thank you very much",
    "thanks",
    "thanks for watching",
)

_WEAK_WITNESS: frozenset[str] = frozenset({"contradict", "lost", "unheard"})

_PHRASES_RE = re.compile(
    r"^(?:(?:"
    + "|".join(re.escape(p) for p in sorted(HALLUCINATION_PHRASES, key=len, reverse=True))
    + r")(?: |$))+$"
)


@dataclass(frozen=True)
class TailSanitizeReport:
    dropped: tuple[tuple[int, str, str], ...]


def _normalize_text(text: str) -> str:
    cleaned = re.sub(r"[.,!?\"']", "", text.strip().lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def _is_phrase_sequence(entries: Sequence[Mapping[str, Any]]) -> bool:
    """True when the words, joined, are one or more hallucination phrases."""
    joined = " ".join(_normalize_text(str(e.get("word", ""))) for e in entries)
    joined = re.sub(r"\s+", " ", joined).strip()
    return bool(joined) and bool(_PHRASES_RE.match(joined))


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _gap_before(prev: Mapping[str, Any], first: Mapping[str, Any]) -> float | None:
    """Silence between ``prev`` ending and ``first`` starting, or None if unmeasurable."""
    # No fallback to prev start_s: start-to-start is not silence (LYR-08).
    prev_end = _number(prev.get("end_s"))
    first_start = _number(first.get("start_s"))
    if prev_end is None or first_start is None:
        return None
    return first_start - prev_end


def _hallucination_suffix_len(kept: Sequence[Mapping[str, Any]]) -> int:
    """Length of the trailing span rule 2 drops, or 0 when evidence is lacking."""
    last_final = -1
    for i, entry in enumerate(kept):
        if entry.get("line_final") is True:
            last_final = i
    if last_final < 0:
        return 0
    # Longest trailing run of weak-witness words after the last sung line (every
    # word past ``last_final`` is non-line-final by construction).
    start = len(kept)
    while start - 1 > last_final:
        if kept[start - 1].get("witness") not in _WEAK_WITNESS:
            break
        start -= 1
    # Shrink from the front until the span is a pure phrase sequence with a
    # measured silence gap before it; nothing smaller than a full phrase drops.
    while start < len(kept):
        span = kept[start:]
        if _is_phrase_sequence(span):
            gap = _gap_before(kept[start - 1], span[0])
            if gap is not None and gap >= HALLUCINATION_GAP_S:
                return len(span)
        start += 1
    return 0


def _duration_limit(duration_s: float | None) -> float | None:
    """Return the actual track duration, or raise on a malformed non-None value."""
    if duration_s is None:
        return None
    if isinstance(duration_s, bool) or not isinstance(duration_s, (int, float)) or duration_s <= 0:
        raise ValueError(f"duration_s must be a positive number or None, got {duration_s!r}")
    return float(duration_s)


def _is_past_duration(entry: Mapping[str, Any], duration: float | None) -> bool:
    if duration is None:
        return False
    start = _number(entry.get("start_s"))
    return start is not None and start >= duration


def sanitize_words_for_artifact(
    words: Sequence[Mapping[str, Any]],
    *,
    duration_s: float | None,
) -> tuple[list[Mapping[str, Any]], TailSanitizeReport]:
    """Return words safe to pass to ``karaoke_cache.build_words``."""
    duration = _duration_limit(duration_s)
    dropped: list[tuple[int, str, str]] = []
    kept: list[Mapping[str, Any]] = []
    kept_orig_idx: list[int] = []
    for index, entry in enumerate(words):
        if _is_past_duration(entry, duration):
            dropped.append((index, str(entry.get("word", "")), "past_duration"))
            continue
        kept.append(entry)
        kept_orig_idx.append(index)

    trim = _hallucination_suffix_len(kept)
    if trim:
        cut = len(kept) - trim
        dropped.extend(
            (kept_orig_idx[j], str(kept[j].get("word", "")), "post_final_hallucination")
            for j in range(cut, len(kept))
        )
        kept = kept[:cut]

    dropped.sort(key=lambda d: d[0])
    return kept, TailSanitizeReport(dropped=tuple(dropped))


__all__ = [
    "HALLUCINATION_GAP_S",
    "HALLUCINATION_PHRASES",
    "TailSanitizeReport",
    "sanitize_words_for_artifact",
]
