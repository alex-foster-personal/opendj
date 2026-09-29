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

from apps.analysis.canonical import key_lane_stale_but_unpromoted

from .own_lane_store import (
    SOURCE_OWN,
    canonical_lane_result,
    effective_lane_source,
    state_conn_ro,
    table_present,
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


def _segments_block(
    result: Any, stable_id: str, *, state_db_path: Path | None = None
) -> dict[str, Any]:
    """The wire block for one own key lane result, or the `missing` shape."""
    if result is None:
        return {
            "status": "missing",
            "reason": OWN_KEY_MISSING_REASON,
            "segments": [],
        }
    if result.status != "ok":
        return {"status": result.status, "reason": result.reason, "segments": []}
    depends_on = result.payload.get("depends_on", {}).get("beatgrid")
    if isinstance(depends_on, dict):
        from apps.analysis.backends.own_key import canonical_beatgrid_record
        from apps.analysis_key.lane_payload import (
            REASON_STALE_DEPENDENCY,
            beatgrid_dependency_matches,
            beatgrid_identity_from_record,
        )

        current = canonical_beatgrid_record(stable_id, db_path=state_db_path)
        if current is None or not beatgrid_dependency_matches(
            depends_on, beatgrid_identity_from_record(current)
        ):
            return {
                "status": "missing",
                "reason": REASON_STALE_DEPENDENCY,
                "segments": [],
            }
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


def _set_dynamic_key_hint(payload: dict[str, Any], *, present: bool) -> None:
    """Reconcile `performance_hints.dynamic_key` for every own-key outcome."""
    existing = payload.get("performance_hints")
    hints = dict(existing) if isinstance(existing, dict) else {}
    if present:
        hints["dynamic_key"] = True
    else:
        hints.pop("dynamic_key", None)
    if hints:
        payload["performance_hints"] = hints
    else:
        payload.pop("performance_hints", None)


def apply_own_key_segments(
    payload: dict[str, Any],
    stable_id: str,
    state_db_path: Path | None = None,
    *,
    has_rb_mapping: bool | None = None,
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
        if (
            effective_lane_source(conn, OWN_KEY_LANE, has_rb_mapping=has_rb_mapping)
            != SOURCE_OWN
        ):
            payload.pop("key_segments", None)
            _set_dynamic_key_hint(payload, present=False)
            return payload
        result = canonical_lane_result(conn, stable_id, OWN_KEY_LANE)
        # Same absent-schema rule as `canonical_lane_result`: no
        # `analysis_canonical` means no lane was ever promoted, so nothing can
        # be stale-but-unpromoted. Unguarded, an unmapped track (key lane now
        # `own` by default, STANDALONE-06) 500'd `/anlz` on a state DB that
        # has never run the analysis pipeline.
        stale_unpromoted = (
            result is None
            and conn is not None
            and table_present(conn, "analysis_canonical")
            and key_lane_stale_but_unpromoted(conn, stable_id)
        )
    finally:
        if conn is not None:
            conn.close()
    if stale_unpromoted:
        from apps.analysis_key.lane_payload import REASON_STALE_DEPENDENCY

        wire_block = {
            "status": "missing",
            "reason": REASON_STALE_DEPENDENCY,
            "segments": [],
        }
    else:
        wire_block = _segments_block(result, stable_id, state_db_path=state_db_path)
    payload["key_segments"] = wire_block
    dynamic = (
        wire_block.get("status") == "ok"
        and len(wire_block.get("segments", ())) >= 2
    )
    _set_dynamic_key_hint(payload, present=dynamic)
    return payload


__all__ = [
    "NO_TONAL_CENTER",
    "OWN_KEY_LANE",
    "OWN_KEY_MISSING_REASON",
    "apply_own_key_segments",
]
