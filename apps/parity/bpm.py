"""BPM lane: djmdContent.BPM (integer, BPM x100) vs own analysis.bpm."""

from __future__ import annotations

from typing import Any

from apps.parity.figure import LaneFigure
from apps.parity.lanes import DENOMINATOR_NAME


def _is_octave(rb_x100: int, own_x100: int) -> bool:
    """Half or double at the 0.01 BPM storage grain, nothing else."""
    if rb_x100 <= 0 or own_x100 <= 0:
        return False
    return own_x100 * 2 == rb_x100 or rb_x100 * 2 == own_x100


def score_bpm(rows: list[dict[str, Any]], *, measured_at: str) -> LaneFigure:
    """Score present rows that carry a positive rekordbox BPM x100."""
    ungradable_ids: list[str] = []
    no_own_ids: list[str] = []
    agree_ids: list[str] = []
    disagree_ids: list[str] = []
    octave_ids: list[str] = []
    within_0_1 = 0
    within_1_0 = 0
    for row in rows:
        sid = row["stable_id"]
        rb_x100 = row.get("rb_bpm_x100")
        if not isinstance(rb_x100, int) or isinstance(rb_x100, bool) or rb_x100 <= 0:
            ungradable_ids.append(sid)
            continue
        own = row.get("own_bpm")
        if own is None:
            no_own_ids.append(sid)
            continue
        rb_bpm = rb_x100 / 100.0
        own_x100 = round(float(own) * 100)
        delta = abs(float(own) - rb_bpm)
        if own_x100 == rb_x100:
            agree_ids.append(sid)
        elif _is_octave(rb_x100, own_x100):
            octave_ids.append(sid)
            disagree_ids.append(sid)
        else:
            disagree_ids.append(sid)
        if delta <= 0.1:
            within_0_1 += 1
        if delta <= 1.0:
            within_1_0 += 1
    denominator_n = len(rows) - len(ungradable_ids)
    scored_n = len(agree_ids) + len(disagree_ids)
    return LaneFigure(
        lane="bpm",
        status="scored",
        measured_at=measured_at,
        denominator_name=DENOMINATOR_NAME["bpm"],
        denominator_n=denominator_n,
        scored_n=scored_n,
        exact_n=len(agree_ids),
        within_0_1_n=within_0_1,
        within_1_0_n=within_1_0,
        no_own_n=len(no_own_ids),
        agree_ids=tuple(agree_ids),
        disagree_ids=tuple(disagree_ids),
        octave_ids=tuple(octave_ids),
        no_own_ids=tuple(no_own_ids),
        ungradable_ids=tuple(ungradable_ids),
        ungradable={"missing_rb_bpm": len(ungradable_ids)},
    )
