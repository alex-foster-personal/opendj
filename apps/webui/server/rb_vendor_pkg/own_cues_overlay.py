"""Serve the own cue store's cues on `/anlz` (CUES-01).

Every `/anlz` branch fills ``payload["cues"]`` first (rekordbox ``djmdCue``
for a mapped track, ``[]`` for a local import). When the track has its own
``cue_points`` row, that row is the truth and replaces whatever the branch
put there; with no row the branch's value stands, which is the same fallback
the hot-cue routes use.

-Claude
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from apps.shared.state import cue_store

from .own_lane_store import state_conn_ro, table_present


def apply_own_cues(
    payload: dict[str, Any],
    stable_id: str,
    state_db_path: Path | None = None,
) -> dict[str, Any]:
    """Replace ``payload["cues"]`` with the own set when one is stored."""
    conn = state_conn_ro(state_db_path)
    if conn is None:
        return payload
    try:
        if not table_present(conn, "track_fields"):
            return payload
        own = cue_store.stored_cues_view(conn, stable_id)
    finally:
        conn.close()
    if own is not None:
        payload["cues"] = own
    return payload


__all__ = ["apply_own_cues"]
