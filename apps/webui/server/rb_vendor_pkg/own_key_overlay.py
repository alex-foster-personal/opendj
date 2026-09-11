"""Own key-change segment projection (native-analysis v1, NATIVE-05).

`GET /anlz` gains `key_segments: {status, reason, segments: [...]}` when the
`key` lane's effective source is own. That is the whole addition: the SCALAR
key does not ride this payload -- it is read-time projected through
`analysis_projection`/`effective_fields` (spec section 3, "Consumer, key and
loudness lanes (scalar values)") -- while the segments are timeline data, which
is exactly what `/anlz` serves. The two paths are separate on purpose and this
module touches only the second.

The block carries its OWN status because key-change analysis needs own
downbeats and can be `missing` while the scalar key succeeded: `ok` with one
segment is a stable key, `ok` with two or more is a real change (the deck sets
`performance_hints.dynamic_key` from that, which the beatgrid overlay's
`_set_dynamic_tempo_hint` twin does for tempo), `missing` means the change
analysis has not run, `failed` carries the analyzer's reason.

Applied AFTER the file cache, beside the beatgrid overlay and for its reason: the
cache is keyed on the ANLZ mtime and the points parameter, neither of which
moves when an own record is written or the source toggle flips. And the field
is REMOVED when the source is rekordbox rather than merely left unwritten, so a
cache entry written while own was selected cannot keep serving segments under a
rekordbox selection.

-Claude
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .own_lane_store import (
    SOURCE_OWN,
    canonical_lane_result,
    effective_lane_source,
    state_conn_ro,
)

#: The lane name as `apps.analysis.lanes` spells it, and as the record's lane
#: key. Spelled out because an own lane's canonical pointer is per SELECTION
#: lane, and a typo here would silently always answer `missing`.
OWN_KEY_LANE = "key"

#: No own key record yet: the queue is the fix, not a tooltip (spec section 3).
OWN_KEY_MISSING_REASON = "no own key analysis record for this track yet"

#: The lane's named failure reason, prefixed into the record's lane reason.
NO_TONAL_CENTER = "no_tonal_center"

_SEGMENT_KEYS = (
    "start_bar", "end_bar", "start_s", "end_s",
    "key_camelot", "key_openkey", "confidence",
)


def _segments_block(result: Any, stable_id: str) -> dict[str, Any]:
    """The wire block for one own key lane result, or the `missing` shape."""
    if result is None:
        return {
            "status": "missing",
            "reason": OWN_KEY_MISSING_REASON,
            "segments": [],
        }
    if result.status != "ok":
        return {"status": result.status, "reason": result.reason, "segments": []}
    block = result.payload["segments"]
    if not isinstance(block, Mapping):
        raise TypeError(
            f"own key record for {stable_id} carries a {type(block).__name__} "
            "segments block, not a mapping; the record contract requires one"
        )
    if block.get("status") == "ok" and not block.get("segments"):
        # Spec section 3: "A bare absent array is not a state." The store's own
        # validator refuses to write one and the read side refuses to serve
        # one: an `ok` block with no segments would silently disable the deck's
        # key-change markers while reading as a healthy analysis.
        raise RuntimeError(
            f"own key record for {stable_id} is status ok with no segments, "
            "which is a contract violation and not a state"
        )
    return {
        "status": block["status"],
        "reason": block.get("reason"),
        "segments": [
            {key: segment[key] for key in _SEGMENT_KEYS}
            for segment in block.get("segments", ())
        ],
    }


def apply_own_key_segments(
    payload: dict[str, Any], stable_id: str, state_db_path: Path | None = None
) -> dict[str, Any]:
    """Add (or remove) `key_segments` per the effective source. Mutates `payload`.

    A rekordbox selection REMOVES the field rather than leaving it alone:
    reconciliation, not a conditional write, is what makes the flip safe
    against a cached payload (`_set_dynamic_tempo_hint` in the beatgrid overlay
    makes the same argument for `dynamic_tempo`).

    ``state_db_path`` is forwarded to `state_conn_ro`; see its docstring.
    """
    conn = state_conn_ro(state_db_path)
    try:
        if effective_lane_source(conn, OWN_KEY_LANE) != SOURCE_OWN:
            payload.pop("key_segments", None)
            return payload
        result = canonical_lane_result(conn, stable_id, OWN_KEY_LANE)
    finally:
        if conn is not None:
            conn.close()
    payload["key_segments"] = _segments_block(result, stable_id)
    return payload


__all__ = [
    "NO_TONAL_CENTER",
    "OWN_KEY_LANE",
    "OWN_KEY_MISSING_REASON",
    "apply_own_key_segments",
]
