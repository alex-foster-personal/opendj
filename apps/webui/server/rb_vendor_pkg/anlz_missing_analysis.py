"""Rescue for rekordbox-mapped tracks with no AnalysisDataPath (issue #2346).

When ``djmdContent.AnalysisDataPath`` is null or empty, ``anlz_dir()`` raises
``ANALYSIS_NOT_FOUND`` even though the audio is present. That is not GUARD-01
H10 (path present but broken); it means rekordbox never analyzed the row.
Serve ``local_anlz_payload`` and optionally overlay a measured own beatgrid
even when the launch source is still ``rbx``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from apps.adapters.rekordbox.models import RbContent

from .own_beatgrid_overlay import (
    _own_beatgrid_block,
    _own_beatgrid_lane_result,
    _set_dynamic_tempo_hint,
    _state_conn_ro,
)


def vendor_analysis_path_absent(content: RbContent) -> bool:
    """True when rekordbox recorded no AnalysisDataPath (None or empty)."""
    return not content.analysis_data_path


def apply_own_beatgrid_when_vendor_absent(
    payload: dict[str, Any], stable_id: str, state_db_path: Path | None
) -> dict[str, Any]:
    """If an own beatgrid record exists, overlay it even when the lane is rbx.

    Missing own record: leave the rekordbox-empty block so /beatgrid-fallback
    can still run. Do NOT stamp source=own status=missing (that blocks the
    legacy fallback; beatgrid-fallback.ts).
    Failed own record: stamp failed with reason (NATIVE-01 inert contract).
    """
    conn = _state_conn_ro(state_db_path)
    try:
        result = _own_beatgrid_lane_result(conn, stable_id)
    finally:
        if conn is not None:
            conn.close()
    if result is None:
        return payload
    block, tempo_changes = _own_beatgrid_block(result, stable_id)
    payload["beatgrid"] = block
    payload["tempo_changes"] = tempo_changes
    _set_dynamic_tempo_hint(payload, present=bool(tempo_changes))
    return payload
