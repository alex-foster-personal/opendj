"""Estimate beats per bar from downbeat spacing on the beat grid.

Beat This! emits downbeat times; the spacing between consecutive downbeat anchors
in beats is a direct vote on meter. The default remains four when the vote is not
confident, and `beats_per_bar_status` names whether that number was measured or is
only the default.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from apps.analysis_beatgrid.bar_phase import (
    BAR_BEATS,
    DOWNBEAT_MATCH_TOLERANCE_S,
    closed_bar_beat_lengths,
    downbeat_anchor_indices,
)

# ----- Named constants ----------------------------------------------------

DEFAULT_BEATS_PER_BAR = BAR_BEATS

STATUS_MEASURED = "measured"
STATUS_UNMEASURED = "unmeasured"

COMPOUND_METER_NOTE_6_8 = "compound_6_8_interpreted_as_3"

# Lengths the vote will return as measured winners. Others force unmeasured
# rather than guessing an exotic meter.
_PLAUSIBLE_BEATS_PER_BAR = frozenset({2, 3, 4, 5, 6, 7, 8, 9, 12})

__all__ = [
    "COMPOUND_METER_NOTE_6_8",
    "DEFAULT_BEATS_PER_BAR",
    "MeterEstimate",
    "STATUS_MEASURED",
    "STATUS_UNMEASURED",
    "estimate_beats_per_bar",
]


@dataclass(frozen=True)
class MeterEstimate:
    """Beats per bar plus whether the spacing vote actually chose it."""

    beats_per_bar: int
    beats_per_bar_status: str
    compound_meter_note: str | None = None
    n_closed_bars: int = 0
    spacing_vote: int | None = None


def _strict_majority_winner(lengths: list[int]) -> int | None:
    """The length that strictly outvotes every other closed bar, if one exists."""
    if not lengths:
        return None
    counts = Counter(lengths)
    top_count = max(counts.values())
    winners = [length for length, count in counts.items() if count == top_count]
    if len(winners) != 1:
        return None
    if top_count <= len(lengths) / 2:
        return None
    winner = winners[0]
    if winner not in _PLAUSIBLE_BEATS_PER_BAR:
        return None
    return winner


def estimate_beats_per_bar(
    beat_times: Sequence[float],
    downbeat_times: Sequence[float],
    *,
    tolerance_s: float = DOWNBEAT_MATCH_TOLERANCE_S,
) -> MeterEstimate:
    """Vote on closed bar lengths in beats between downbeat anchors.

     Raises when `beat_times` is not strictly ascending or a downbeat is not on
    a beat, matching `assign_bar_phase`.
    """
    if not beat_times:
        return MeterEstimate(DEFAULT_BEATS_PER_BAR, STATUS_UNMEASURED)

    anchors = downbeat_anchor_indices(beat_times, downbeat_times, tolerance_s=tolerance_s)
    if len(anchors) < 2:
        return MeterEstimate(DEFAULT_BEATS_PER_BAR, STATUS_UNMEASURED)
    closed = closed_bar_beat_lengths(anchors, len(beat_times))
    if not closed:
        return MeterEstimate(DEFAULT_BEATS_PER_BAR, STATUS_UNMEASURED)

    winner = _strict_majority_winner(closed)
    if winner is None:
        return MeterEstimate(
            DEFAULT_BEATS_PER_BAR,
            STATUS_UNMEASURED,
            n_closed_bars=len(closed),
        )

    return MeterEstimate(
        winner,
        STATUS_MEASURED,
        compound_meter_note=None,
        n_closed_bars=len(closed),
        spacing_vote=winner,
    )
