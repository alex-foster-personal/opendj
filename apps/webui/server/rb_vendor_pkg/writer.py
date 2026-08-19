"""The hot-cue read-write surface into rekordbox's master.plain.db.

Moved from ``apps/webui/server/rb_vendor.py`` C10 (source lines 1657-1666,
1769-1804, 1879-1916, 2033-2220) per ``.planning/t3b-decomposition-map.md``
section 2 row 12. Everything above C10 in the legacy file opened the DB
``mode=ro`` with ``PRAGMA query_only``; this module is the only writer.

Writes stop at Kind 8 (slot H) on purpose -- Kind 9-11 rows are observed in the
wild (RECON-DATA.md section 4, 24 rows) but their slot mapping is unverified,
so this surface never guesses at it (PARITY-TODO.md "Hot-cue SAVE").

master.plain.db here is the STATIC decrypted working copy (CLAUDE.md /
PARITY-TODO.md "Known data notes"), not the live rekordbox db the desktop app
has open -- SAVE round-trips through our own read path (``fetch_cues`` is
always re-queried live, never cached) but does not sync back to rekordbox
itself; that is the separate write-back-rekordbox-djay node.

Quantizing to the beatgrid (DEPENDENCY-PATH "quantized position") is a frontend
concern (beat-sync-math.quantizeToNearestBeat against the loaded AnlzBeatgrid)
-- this surface accepts whatever ``in_ms`` it is given verbatim.

**Connections are injected, never resolved here.** Each entry point takes a
zero-argument factory, so the adapter carries no opinion about which file it
writes and no module-global path to monkeypatch. ``rb_vendor.py`` supplies
factories that read ``MASTER_PLAIN_DB`` and ``_open_rw`` from its own module
namespace at call time, which is what keeps its two long-standing test seams
(``monkeypatch.setattr(rb_vendor, "MASTER_PLAIN_DB", ...)`` and
``monkeypatch.setattr(rb_vendor, "_open_rw", ...)``) working through the move.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from .cues import (
    HOT_CUE_SLOTS,
    _cue_revision,
    _cue_view,
    _live_slot_snapshot,
    _msec_to_frame,
    _new_cue_id,
    _rb_timestamp,
    _require_current_revision,
    _slot_to_kind,
    _validate_cue_position,
)
from .reversal import (
    _bump_slot_generation,
    _create_reversal,
    _ensure_reversal_tables,
    _load_reversal,
    _slot_generation,
)

#: Zero-argument factory returning an open connection to master.plain.db.
ConnectMaster = Callable[[], sqlite3.Connection]

_KINDS = tuple(range(1, len(HOT_CUE_SLOTS) + 1))


def _open_rw(path: Path, label: str) -> sqlite3.Connection:
    if not path.exists():
        raise HTTPException(
            status_code=500,
            detail={
                "code": f"{label}_UNAVAILABLE",
                "message": f"required database missing on disk: {path}",
            },
        )
    return sqlite3.connect(str(path))


def fetch_hot_cue_slots(
    vendor_id: str,
    *,
    open_rw: ConnectMaster,
) -> list[dict[str, Any]]:
    """Read all eight slot states, including revisions for empty slots."""
    master = open_rw()
    try:
        master.execute("BEGIN IMMEDIATE")
        _ensure_reversal_tables(master)
        snapshots = {
            kind: _live_slot_snapshot(master, vendor_id, kind) for kind in _KINDS
        }
        generations = {
            kind: _slot_generation(master, vendor_id, kind) for kind in _KINDS
        }
        master.commit()
    except Exception:
        master.rollback()
        raise
    finally:
        master.close()
    return [
        {
            "slot": slot,
            "cue": _cue_view(
                slot,
                snapshots[kind],
                _cue_revision(vendor_id, kind, generations[kind], snapshots[kind]),
            )
            if snapshots[kind]
            else None,
            "revision": _cue_revision(
                vendor_id, kind, generations[kind], snapshots[kind]
            ),
        }
        for kind, slot in enumerate(HOT_CUE_SLOTS, start=1)
    ]


def _restore_snapshot(
    conn: sqlite3.Connection,
    vendor_id: str,
    kind: int,
    snapshot: Mapping[str, Any],
    now: str,
) -> None:
    result = conn.execute(
        "UPDATE djmdCue SET InMsec = ?, InFrame = ?, InMpegFrame = ?, "
        "InMpegAbs = ?, OutMsec = ?, OutFrame = ?, ActiveLoop = ?, "
        "BeatLoopSize = ?, ColorTableIndex = ?, Comment = ?, "
        "rb_local_deleted = 0, updated_at = ? "
        "WHERE ID = ? AND ContentID = ? AND Kind = ?",
        (
            snapshot["in_ms"],
            snapshot["in_frame"],
            snapshot["in_mpeg_frame"],
            snapshot["in_mpeg_abs"],
            snapshot["out_ms"],
            snapshot["out_frame"],
            int(snapshot["active_loop"]),
            snapshot["beat_loop_size"],
            snapshot["color_table_index"],
            snapshot["comment"],
            now,
            snapshot["id"],
            vendor_id,
            kind,
        ),
    )
    if result.rowcount != 1:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "HOT_CUE_PREIMAGE_UNRESTORABLE",
                "message": "the original cue row no longer exists",
            },
        )


def save_hot_cue(
    vendor_id: str,
    slot: str,
    in_ms: int,
    *,
    expected_revision: str,
    comment: str | None = None,
    color_table_index: int | None = None,
    open_rw: ConnectMaster,
) -> dict[str, Any]:
    """CAS-save a hot cue and return its atomic preimage for one-step undo."""
    kind = _slot_to_kind(slot)
    now = _rb_timestamp()
    master = open_rw()
    try:
        master.execute("BEGIN IMMEDIATE")
        _validate_cue_position(master, vendor_id, in_ms)
        preimage = _live_slot_snapshot(master, vendor_id, kind)
        generation = _slot_generation(master, vendor_id, kind)
        _require_current_revision(
            expected_revision,
            _cue_revision(vendor_id, kind, generation, preimage),
        )
        if preimage is not None:
            update = master.execute(
                "UPDATE djmdCue SET InMsec = ?, InFrame = ?, InMpegFrame = NULL, "
                "InMpegAbs = NULL, OutMsec = NULL, OutFrame = NULL, "
                "ActiveLoop = 0, ColorTableIndex = ?, Comment = ?, "
                "updated_at = ? WHERE ID = ? AND ContentID = ? AND Kind = ?",
                (
                    in_ms,
                    _msec_to_frame(in_ms),
                    color_table_index,
                    comment,
                    now,
                    preimage["id"],
                    vendor_id,
                    kind,
                ),
            )
            if update.rowcount != 1:
                raise RuntimeError("save_hot_cue: scoped cue row disappeared")  # noqa: TRY301 -- must raise inside the txn so rollback fires
        else:
            cue_id = _new_cue_id(master)
            master.execute(
                "INSERT INTO djmdCue (ID, ContentID, InMsec, InFrame, "
                "InMpegFrame, InMpegAbs, OutMsec, OutFrame, Kind, "
                "ColorTableIndex, ActiveLoop, Comment, rb_local_deleted, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?, 0, ?, 0, "
                "?, ?)",
                (
                    cue_id,
                    vendor_id,
                    in_ms,
                    _msec_to_frame(in_ms),
                    kind,
                    color_table_index,
                    comment,
                    now,
                    now,
                ),
            )
        current = _live_slot_snapshot(master, vendor_id, kind)
        if current is None:
            raise RuntimeError("save_hot_cue: committed slot disappeared")  # noqa: TRY301 -- must raise inside the txn so rollback fires
        generation = _bump_slot_generation(master, vendor_id, kind)
        revision = _cue_revision(vendor_id, kind, generation, current)
        reversal_id = _create_reversal(
            master,
            vendor_id,
            kind,
            preimage,
            revision,
        )
        master.commit()
    except Exception:
        master.rollback()
        raise
    finally:
        master.close()
    return {
        "cue": _cue_view(slot, current, revision),
        "reversal": {"reversal_id": reversal_id},
    }


def clear_hot_cue(
    vendor_id: str,
    slot: str,
    *,
    expected_revision: str,
    open_rw: ConnectMaster,
) -> dict[str, Any]:
    """CAS-clear a hot cue and return its atomic preimage for restore."""
    kind = _slot_to_kind(slot)
    master = open_rw()
    try:
        master.execute("BEGIN IMMEDIATE")
        preimage = _live_slot_snapshot(master, vendor_id, kind)
        generation = _slot_generation(master, vendor_id, kind)
        _require_current_revision(
            expected_revision,
            _cue_revision(vendor_id, kind, generation, preimage),
        )
        if preimage is not None:
            update = master.execute(
                "UPDATE djmdCue SET rb_local_deleted = 1, updated_at = ? "
                "WHERE ID = ? AND ContentID = ? AND Kind = ?",
                (_rb_timestamp(), preimage["id"], vendor_id, kind),
            )
            if update.rowcount != 1:
                raise RuntimeError("clear_hot_cue: scoped cue row disappeared")  # noqa: TRY301 -- must raise inside the txn so rollback fires
        current = _live_slot_snapshot(master, vendor_id, kind)
        generation = _bump_slot_generation(master, vendor_id, kind)
        revision = _cue_revision(vendor_id, kind, generation, current)
        reversal_id = _create_reversal(
            master,
            vendor_id,
            kind,
            preimage,
            revision,
        )
        master.commit()
    except Exception:
        master.rollback()
        raise
    finally:
        master.close()
    return {
        "cue": None,
        "revision": revision,
        "reversal": {"reversal_id": reversal_id},
    }


def restore_hot_cue(
    vendor_id: str,
    slot: str,
    *,
    expected_revision: str,
    reversal_id: str,
    open_rw: ConnectMaster,
) -> dict[str, Any]:
    """CAS-restore one server-authoritative, single-use reversal token."""
    kind = _slot_to_kind(slot)
    master = open_rw()
    try:
        master.execute("BEGIN IMMEDIATE")
        reversal = _load_reversal(master, reversal_id, vendor_id, kind)
        current = _live_slot_snapshot(master, vendor_id, kind)
        generation = _slot_generation(master, vendor_id, kind)
        revision = _cue_revision(vendor_id, kind, generation, current)
        _require_current_revision(expected_revision, revision)
        if reversal["post_revision"] != revision:
            raise HTTPException(  # noqa: TRY301 -- rollback depends on this
                status_code=409,
                detail={
                    "code": "HOT_CUE_REVERSAL_STALE",
                    "message": "hot-cue slot changed after the reversible mutation",
                    "current_revision": revision,
                },
            )
        preimage = reversal["preimage"]
        if preimage is None:
            if current is not None:
                master.execute(
                    "UPDATE djmdCue SET rb_local_deleted = 1, updated_at = ? "
                    "WHERE ID = ? AND ContentID = ? AND Kind = ?",
                    (_rb_timestamp(), current["id"], vendor_id, kind),
                )
        else:
            _restore_snapshot(master, vendor_id, kind, preimage, _rb_timestamp())
        master.execute(
            "UPDATE rb_hot_cue_reversal SET ConsumedAt = ? "
            "WHERE ID = ? AND ContentID = ? AND Kind = ? AND ConsumedAt IS NULL",
            (_rb_timestamp(), reversal_id, vendor_id, kind),
        )
        restored = _live_slot_snapshot(master, vendor_id, kind)
        generation = _bump_slot_generation(master, vendor_id, kind)
        revision = _cue_revision(vendor_id, kind, generation, restored)
        master.commit()
    except Exception:
        master.rollback()
        raise
    finally:
        master.close()
    return {
        "cue": _cue_view(slot, restored, revision) if restored else None,
        "revision": revision,
    }
