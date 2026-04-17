"""PLAY IT entry point: solve + persist."""
from __future__ import annotations

import sqlite3

from apps.shared.harmonic import TrackFeature
from apps.shared.play_orders import (
    add_entry,
    create_play_order,
    delete_play_order,
)

from .set_goal import SetGoal, set_goal_to_json
from .solver import SolveResult, suggest_order

_MISSING_FIELD_LIMIT: float = 0.05


class InsufficientDataError(Exception):
    """Raised when the playlist has too many missing analysis fields."""

    def __init__(
        self, *, playlist_id: str, missing: dict[str, list[str]]
    ) -> None:
        self.playlist_id = playlist_id
        self.missing = missing
        parts = [
            f"{field}: {', '.join(sids[:5])}{'...' if len(sids) > 5 else ''}"
            for field, sids in missing.items()
            if sids
        ]
        super().__init__(
            f"playlist {playlist_id!r} failed PLAY IT pre-flight: "
            + "; ".join(parts)
        )


def _check_coverage(playlist_id: str, tracks: list[TrackFeature]) -> None:
    missing: dict[str, list[str]] = {"bpm": [], "key": [], "energy": []}
    for t in tracks:
        if t.bpm is None:
            missing["bpm"].append(t.stable_id)
        if not t.key_camelot:
            missing["key"].append(t.stable_id)
        if t.energy is None:
            missing["energy"].append(t.stable_id)
    total = max(1, len(tracks))
    over_threshold = any(
        len(sids) / total > _MISSING_FIELD_LIMIT
        for sids in missing.values()
    )
    if over_threshold:
        raise InsufficientDataError(playlist_id=playlist_id, missing=missing)


def play_it(
    *,
    conn: sqlite3.Connection,
    playlist_id: str,
    goal: SetGoal,
    tracks: list[TrackFeature],
    name: str = "PLAY IT",
    overwrite: bool = False,
) -> tuple[int, SolveResult]:
    """Solve a PLAY IT ordering and persist it; return (id, result)."""
    _check_coverage(playlist_id, tracks)

    if overwrite:
        delete_play_order(conn, playlist_id, name)

    result = suggest_order(tracks=tracks, goal=goal)
    if not result.order:
        raise ValueError(f"solver produced no ordering for {playlist_id!r}")

    po_id = create_play_order(
        conn,
        playlist_id,
        name,
        generated_by="play-it",
        goal_json=set_goal_to_json(goal),
    )
    trace_by_pos = {t.position: t for t in result.per_step_trace}
    for pos, stable_id in enumerate(result.order):
        hint = (
            trace_by_pos[pos].transition_hint
            if pos in trace_by_pos
            else "start"
        )
        add_entry(conn, po_id, stable_id, pos, transition_hint=hint)
    return po_id, result
