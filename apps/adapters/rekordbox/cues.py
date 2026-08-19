"""Hot-cue modelling and validation -- pure, owns no connection.

Moved verbatim from ``apps/webui/server/rb_vendor.py`` C10 (source lines
1613-1766 and 1807-1876) per ``.planning/t3b-decomposition-map.md`` section 2
row 10. Everything here either transforms a row/dict or reads through a
caller-supplied connection; nothing opens, commits, or closes one.

D3 (map section 3): the 44.1 kHz frame heuristic that used to live here as
``_msec_to_frame`` now has one definition, :func:`apps.shared.rb_frames.msec_to_frame`,
which :mod:`.writer` imports directly. That module's docstring records why the
shared core rather than the map's nominated home.

Seam for S0: :func:`_new_cue_id`, :func:`_live_slot_snapshot`,
:func:`_require_current_revision`, :func:`_duration_ms` and
:func:`_validate_cue_position` still raise ``fastapi.HTTPException`` exactly as
they did in ``rb_vendor``. Replacing it with the domain error type is
``adapters/rekordbox/errors.py``, which the map assigns to slice S0 -- doing it
here would fork that decision and change every asserted status code.

Wave 4 (S8) moved this module from the interim
``apps/webui/server/rb_vendor_pkg/`` staging location to the mapped home. The
body is unchanged.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from collections.abc import Mapping
from typing import Any

from fastapi import HTTPException

# Writes stop at Kind 8 (slot H) on purpose -- Kind 9-11 rows are observed in
# the wild (RECON-DATA.md section 4, 24 rows) but their slot mapping is
# unverified, so this surface never guesses at it (PARITY-TODO.md "Hot-cue
# SAVE"). Canonical home: this is cue-domain vocabulary, not a path constant,
# so it lives with the cue model rather than in the path/config cluster.
HOT_CUE_SLOTS: str = "ABCDEFGH"


class HotCueSlotError(ValueError):
    """Unknown/unsupported hot-cue slot letter."""


def _slot_to_kind(slot: str) -> int:
    if len(slot) != 1 or slot not in HOT_CUE_SLOTS:
        raise HotCueSlotError(
            f"unsupported hot-cue slot {slot!r}; only {HOT_CUE_SLOTS} are "
            "write-supported (Kind 9-11 slot mapping unverified, see "
            "PARITY-TODO.md 'Hot-cue SAVE')"
        )
    return HOT_CUE_SLOTS.index(slot) + 1


def _rb_timestamp() -> str:
    now = time.time()
    struct_time = time.gmtime(now)
    millis = int((now - int(now)) * 1000)
    return time.strftime("%Y-%m-%d %H:%M:%S", struct_time) + f".{millis:03d} +00:00"


def _new_cue_id(conn: sqlite3.Connection) -> str:
    for _ in range(50):
        candidate = str(secrets.randbelow(9_000_000_000) + 1_000_000_000)
        if (
            conn.execute("SELECT 1 FROM djmdCue WHERE ID = ?", (candidate,)).fetchone()
            is None
        ):
            return candidate
    raise HTTPException(
        status_code=500,
        detail={
            "code": "CUE_ID_EXHAUSTED",
            "message": "could not allocate a unique djmdCue ID",
        },
    )


_CUE_SNAPSHOT_COLUMNS = (
    "ID, Kind, InMsec, InFrame, InMpegFrame, InMpegAbs, OutMsec, OutFrame, "
    "ActiveLoop, BeatLoopSize, ColorTableIndex, Comment, updated_at"
)


def _cue_snapshot_from_row(row: tuple[Any, ...]) -> dict[str, Any]:
    """Serialize every destructive hot-cue field needed for an exact undo."""
    (
        cue_id,
        kind,
        in_ms,
        in_frame,
        in_mpeg_frame,
        in_mpeg_abs,
        out_ms,
        out_frame,
        active_loop,
        beat_loop_size,
        color_table_index,
        comment,
        updated_at,
    ) = row
    return {
        "id": str(cue_id),
        "kind": int(kind) if kind is not None else None,
        "in_ms": int(in_ms) if in_ms is not None else None,
        "in_frame": int(in_frame) if in_frame is not None else None,
        "in_mpeg_frame": int(in_mpeg_frame) if in_mpeg_frame is not None else None,
        "in_mpeg_abs": int(in_mpeg_abs) if in_mpeg_abs is not None else None,
        "out_ms": int(out_ms) if out_ms is not None else None,
        "out_frame": int(out_frame) if out_frame is not None else None,
        "active_loop": bool(active_loop),
        "beat_loop_size": int(beat_loop_size) if beat_loop_size is not None else None,
        "color_table_index": int(color_table_index)
        if color_table_index is not None
        else None,
        "comment": comment or None,
        "updated_at": str(updated_at) if updated_at is not None else None,
    }


def _cue_revision(
    vendor_id: str,
    kind: int,
    generation: int,
    snapshot: Mapping[str, Any] | None,
) -> str:
    """Opaque CAS token bound to track, slot generation, and exact state."""
    encoded = json.dumps(
        {
            "content_id": vendor_id,
            "kind": kind,
            "generation": generation,
            "snapshot": snapshot,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _live_slot_snapshot(
    conn: sqlite3.Connection,
    vendor_id: str,
    kind: int,
) -> dict[str, Any] | None:
    rows = conn.execute(
        f"SELECT {_CUE_SNAPSHOT_COLUMNS} FROM djmdCue "
        "WHERE ContentID = ? AND Kind = ? AND rb_local_deleted = 0",
        (vendor_id, kind),
    ).fetchall()
    if len(rows) > 1:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "HOT_CUE_SLOT_CORRUPT",
                "message": f"slot Kind {kind} has {len(rows)} live cue rows",
            },
        )
    return _cue_snapshot_from_row(rows[0]) if rows else None


def _cue_view(slot: str, snapshot: Mapping[str, Any], revision: str) -> dict[str, Any]:
    out_ms = snapshot["out_ms"]
    is_loop = bool(out_ms is not None and out_ms > 0)
    return {
        "kind": "hot_cue",
        "slot": slot,
        "in_ms": snapshot["in_ms"],
        "out_ms": out_ms if is_loop else None,
        "is_loop": is_loop,
        "active_loop": snapshot["active_loop"],
        "beat_loop_size": snapshot["beat_loop_size"],
        "color_table_index": snapshot["color_table_index"],
        "comment": snapshot["comment"],
        "revision": revision,
    }


def _require_current_revision(expected_revision: str, current_revision: str) -> None:
    if not expected_revision:
        raise HTTPException(
            status_code=428,
            detail={
                "code": "HOT_CUE_REVISION_REQUIRED",
                "message": "hot-cue mutation requires an If-Match revision",
            },
        )
    if expected_revision != current_revision:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "HOT_CUE_REVISION_CONFLICT",
                "message": "hot-cue slot changed since it was read",
                "current_revision": current_revision,
            },
            headers={"ETag": current_revision},
        )


def _duration_ms(conn: sqlite3.Connection, vendor_id: str) -> int:
    row = conn.execute(
        "SELECT Length FROM djmdContent WHERE ID = ? AND rb_local_deleted = 0",
        (vendor_id,),
    ).fetchone()
    if row is None or row[0] is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "TRACK_DURATION_UNAVAILABLE",
                "message": f"track {vendor_id} has no finite rekordbox duration",
            },
        )
    duration_s = row[0]
    if (
        isinstance(duration_s, bool)
        or not isinstance(duration_s, int)
        or duration_s < 0
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "TRACK_DURATION_UNAVAILABLE",
                "message": f"track {vendor_id} has invalid rekordbox duration {duration_s!r}",
            },
        )
    return duration_s * 1000


def _validate_cue_position(
    conn: sqlite3.Connection, vendor_id: str, in_ms: int
) -> None:
    if isinstance(in_ms, bool) or not isinstance(in_ms, int):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_CUE_POSITION",
                "message": "in_ms must be a finite integer",
            },
        )
    duration_ms = _duration_ms(conn, vendor_id)
    if not 0 <= in_ms <= duration_ms:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_CUE_POSITION",
                "message": f"in_ms must satisfy 0 <= in_ms <= {duration_ms}, got {in_ms}",
            },
        )
