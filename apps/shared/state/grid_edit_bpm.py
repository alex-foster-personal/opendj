"""Anchor/BPM beat time transform for grid editing (issue #11138, parent #183).

Given a served ``{n, bpm, t}`` grid, an anchor time and a new constant tempo,
rebuild every beat's time with the anchor line fixed, drop lines before 0 s,
and stamp each beat with the new ``bpm``. Persistence and ``/anlz`` apply live
in ``apps.shared.state.grid_edit`` (#11139).

-Cursor
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from apps.shared.state.grid_edit import BEATS_PER_BAR, BPM_DECIMALS, GridEditError

__all__ = ["rebuild_beats_at_bpm"]


def _checked_bpm(bpm: float) -> float:
    if not math.isfinite(bpm) or bpm <= 0:
        raise GridEditError(422, "GRID_EDIT_INVALID", f"bpm must be a finite number > 0, got {bpm!r}")
    return round(bpm, BPM_DECIMALS) + 0.0


def _nearest_beat_index(grid: Sequence[Mapping[str, Any]], at_s: float) -> int:
    """Index of the grid line nearest ``at_s``; a tie goes to the earlier line."""
    return min(range(len(grid)), key=lambda i: (abs(float(grid[i]["t"]) - at_s), i))


def rebuild_beats_at_bpm(
    beats: Sequence[Mapping[str, Any]],
    anchor_t: float,
    bpm: float,
) -> list[dict[str, Any]]:
    """Rebuild ``beats`` at ``bpm`` with the line nearest ``anchor_t`` fixed in time."""
    if not beats:
        raise GridEditError(422, "GRID_EDIT_INVALID", "beatgrid has no beats to transform")
    checked_bpm = _checked_bpm(bpm)
    if not math.isfinite(anchor_t):
        raise GridEditError(422, "GRID_EDIT_INVALID", f"anchor_t must be finite, got {anchor_t!r}")
    i_anchor = _nearest_beat_index(beats, anchor_t)
    anchor_time = float(beats[i_anchor]["t"])
    period = 60.0 / checked_bpm
    out: list[dict[str, Any]] = []
    for i, beat in enumerate(beats):
        n = int(beat["n"])
        if not 1 <= n <= BEATS_PER_BAR:
            raise GridEditError(422, "GRID_EDIT_INVALID", f"beat number {n!r} is outside 1..{BEATS_PER_BAR}")
        t = round(anchor_time + (i - i_anchor) * period, 6)
        if t >= 0:
            out.append({**dict(beat), "t": t, "bpm": checked_bpm})
    return out
