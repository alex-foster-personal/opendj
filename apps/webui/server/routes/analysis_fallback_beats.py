"""Helpers for :func:`synthesize_fallback_beats` (complexity split)."""
from __future__ import annotations

from apps.analysis.record import AnalysisRecord

from .analysis import (
    BEATS_PER_BAR,
    FallbackBeatOut,
    _MIN_BEAT_INTERVAL_S,
    _invalid_record,
)


def fallback_emit(
    beats: list[FallbackBeatOut], t: float, n: int, bar_s: float
) -> None:
    beats.append(FallbackBeatOut(
        n=n, bpm=round(240.0 / bar_s, 2), t=round(t, 3),
    ))


def fallback_tail_beats(
    record: AnalysisRecord,
    downbeats: list[float],
    beats: list[FallbackBeatOut],
) -> list[FallbackBeatOut] | None:
    """Extend the grid past the last downbeat at the last measured bar tempo."""
    if len(downbeats) >= 2:
        tail_bar_s = downbeats[-1] - downbeats[-2]
    elif record.bpm > 0:
        tail_bar_s = BEATS_PER_BAR * 60.0 / record.bpm
    else:
        return None
    if tail_bar_s / BEATS_PER_BAR < _MIN_BEAT_INTERVAL_S:
        raise _invalid_record(
            record.stable_id, f"tempo bar of {tail_bar_s:.4f}s implies >1200 BPM"
        )
    t = downbeats[-1]
    n = 1
    while t < record.duration_s:
        fallback_emit(beats, t, n, tail_bar_s)
        t += tail_bar_s / BEATS_PER_BAR
        n = n % BEATS_PER_BAR + 1
    return beats
