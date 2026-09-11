"""Ungradable classification for lanes this round does not score.

A remaining lane still names its ungradable bucket so a later round can
fill scores without changing who is in the denominator. Status is
`not_scored_this_round` or `delegated`; agree and disagree stay empty.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from apps.parity.figure import LaneFigure
from apps.parity.lanes import (
    BEATMAP_OWNER,
    DELEGATED_THIS_ROUND,
    DENOMINATOR_NAME,
    REMAINING_REASON,
    SCORED_THIS_ROUND,
)

_UNGRADABLE_REASON: dict[str, str] = {
    "phrase": "missing_pssi",
    "cues_db": "missing_djmd_cue",
    "cues_anlz": "unreadable_ext",
    "vocal": "missing_pvdi",
}


def _ungradable_pred(lane: str) -> Callable[[dict[str, Any]], bool]:
    if lane == "phrase":
        return lambda row: not row.get("rb_pssi")
    if lane == "cues_db":
        return lambda row: not row.get("rb_cue_db")
    if lane == "cues_anlz":
        return lambda row: not row.get("rb_ext_readable", True)
    if lane == "vocal":
        return lambda row: not row.get("rb_pvdi")
    return lambda _row: False


def classify_remaining(
    lane: str, rows: list[dict[str, Any]], *, measured_at: str
) -> LaneFigure:
    """Name the ungradable set for a lane this round does not score."""
    if lane in SCORED_THIS_ROUND:
        raise ValueError(
            f"lane {lane!r} is scored this round; classify_remaining is for "
            "remaining lanes only"
        )
    pred = _ungradable_pred(lane)
    ungradable_ids = tuple(row["stable_id"] for row in rows if pred(row))
    bucket = _UNGRADABLE_REASON.get(lane)
    ungradable = {bucket: len(ungradable_ids)} if bucket else {}
    if lane in DELEGATED_THIS_ROUND:
        return LaneFigure(
            lane=lane,
            status="delegated",
            measured_at=measured_at,
            denominator_name=DENOMINATOR_NAME[lane],
            denominator_n=0,
            owner=BEATMAP_OWNER,
            reason=(
                f"{BEATMAP_OWNER} owns this lane. PARITY-01 does not restate "
                "a second beat metric."
            ),
        )
    return LaneFigure(
        lane=lane,
        status="not_scored_this_round",
        measured_at=measured_at,
        denominator_name=DENOMINATOR_NAME[lane],
        denominator_n=len(rows) - len(ungradable_ids),
        reason=REMAINING_REASON[lane],
        ungradable_ids=ungradable_ids,
        ungradable=ungradable,
    )
