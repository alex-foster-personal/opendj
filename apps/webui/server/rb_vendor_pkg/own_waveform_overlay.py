"""Own tri-band waveform projection (native-analysis v1, NATIVE-06).

When the ``waveform`` lane's effective source is own, ``GET /anlz`` serves the
canonical own waveform record's tri-band preview and detail instead of rekordbox
PWV*. Phrases are rekordbox PSSI and are computed upstream of this overlay;
this module never reads or writes the ``phrases`` field on the payload.

Applied AFTER the file cache, beside the beatgrid and key overlays: the cache
is keyed on the ANLZ mtime and the points parameter, neither of which moves
when an own record is written or the source toggle flips.

-Claude
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from .own_lane_store import (
    SOURCE_OWN,
    canonical_lane_result,
    effective_lane_source,
    state_conn_ro,
)

OWN_WAVEFORM_LANE = "waveform"
OWN_WAVEFORM_MISSING_REASON = "no own waveform record for this track yet"


def _empty_tri_bands() -> dict[str, Any]:
    return {"length": 0, "low": [], "mid": [], "high": []}


def _waveform_block(result: Any) -> dict[str, Any]:
    """The wire block for one own waveform lane result, or the ``missing`` shape."""
    empty = _empty_tri_bands()
    if result is None:
        return {
            "kind": "tri",
            "status": "missing",
            "reason": OWN_WAVEFORM_MISSING_REASON,
            "preview": dict(empty),
            "detail": dict(empty),
        }
    if result.status != "ok":
        return {
            "kind": "tri",
            "status": result.status,
            "reason": result.reason,
            "preview": dict(empty),
            "detail": dict(empty),
        }
    payload = result.payload
    return {
        "kind": "tri",
        "status": "ok",
        "reason": None,
        "preview": copy.deepcopy(payload["preview"]),
        "detail": copy.deepcopy(payload["detail"]),
    }


def apply_own_waveform(
    payload: dict[str, Any],
    stable_id: str,
    state_db_path: Path | None = None,
    *,
    has_rb_mapping: bool | None = None,
) -> dict[str, Any]:
    """Replace the waveform block with the own record when own is selected.

    A no-op that returns the payload unchanged when the effective source is
    rekordbox. Mutates and returns ``payload``.

    ``state_db_path`` is forwarded to ``state_conn_ro``; see its docstring.
    """
    conn = state_conn_ro(state_db_path)
    try:
        if (
            effective_lane_source(conn, OWN_WAVEFORM_LANE, has_rb_mapping=has_rb_mapping)
            != SOURCE_OWN
        ):
            return payload
        result = canonical_lane_result(conn, stable_id, OWN_WAVEFORM_LANE)
    finally:
        if conn is not None:
            conn.close()
    payload["waveform"] = _waveform_block(result)
    return payload


__all__ = [
    "OWN_WAVEFORM_LANE",
    "OWN_WAVEFORM_MISSING_REASON",
    "apply_own_waveform",
]
