"""Read Mixed In Key energy for ``/anlz``: scalar plus time series from state.db."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Literal

from apps.adapters.rekordbox import config as rb_config
from apps.mik import SOURCE as MIK_SOURCE

EnergySource = Literal["mik", "none"]


class MalformedStoredEnergySegment(ValueError):
    """A row in ``track_energy_segments`` fails serve-time validation."""

    def __init__(self, stable_id: str, seq: int, reason: str) -> None:
        self.stable_id = stable_id
        self.seq = seq
        self.reason = reason
        super().__init__(f"malformed MIK energy segment for {stable_id} seq {seq}: {reason}")


def _scalar_level(conn: sqlite3.Connection, stable_id: str) -> int | None:
    row = conn.execute(
        "SELECT value_json FROM track_fields "
        "WHERE stable_id = ? AND field_name = 'energy' AND source = ? "
        "AND deleted_at IS NULL",
        (stable_id, MIK_SOURCE),
    ).fetchone()
    if row is None:
        return None
    try:
        parsed = json.loads(row[0])
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, bool) or not isinstance(parsed, (int, float)):
        return None
    if not float(parsed).is_integer():
        return None
    level = int(parsed)
    if 1 <= level <= 10:
        return level
    return None


def _wire_segment(
    stable_id: str,
    seq: int,
    start_ms: int,
    length_ms: int,
    energy: int,
    start_clamped: int,
) -> dict[str, Any]:
    if length_ms <= 0:
        raise MalformedStoredEnergySegment(stable_id, seq, "end is before start (length_ms <= 0)")
    if energy < 1 or energy > 10:
        raise MalformedStoredEnergySegment(stable_id, seq, f"level {energy} is outside 1..10")
    start_s = round(start_ms / 1000.0, 3)
    end_s = round((start_ms + length_ms) / 1000.0, 3)
    if end_s < start_s:
        raise MalformedStoredEnergySegment(stable_id, seq, "end is before start")
    return {
        "start_s": start_s,
        "end_s": end_s,
        "level": energy,
        "source": "mik",
        "start_clamped": bool(start_clamped),
    }


def read_mik_energy(conn: sqlite3.Connection, stable_id: str) -> tuple[list[dict[str, Any]], int | None, EnergySource]:
    """Segments in ``seq`` order, MIK scalar level, and ``mik`` or ``none``."""
    rows = conn.execute(
        "SELECT seq, start_ms, length_ms, energy, start_clamped "
        "FROM track_energy_segments WHERE stable_id = ? AND source = ? "
        "ORDER BY seq",
        (stable_id, MIK_SOURCE),
    ).fetchall()
    segments = [
        _wire_segment(stable_id, int(seq), int(start_ms), int(length_ms), int(energy), int(clamped))
        for seq, start_ms, length_ms, energy, clamped in rows
    ]
    scalar = _scalar_level(conn, stable_id)
    if segments or scalar is not None:
        return segments, scalar, "mik"
    return [], None, "none"


def merge_mik_energy(
    payload: dict[str, Any],
    stable_id: str,
    state_db_path: Path | None = None,
) -> dict[str, Any]:
    """Serve-time merge: MIK series and scalar from state.db, never the anlz cache."""
    db_path = state_db_path if state_db_path is not None else rb_config.STATE_DB
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        segments, level, source = read_mik_energy(conn, stable_id)
    finally:
        conn.close()
    return {
        **payload,
        "energy_segments": segments,
        "energy_level": level,
        "energy_source": source,
    }
