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

D5 (map section 3): :func:`fetch_hot_cue_slots` reads like a read and used to
write. It opened **read-write**, ran DDL and an ``INSERT OR IGNORE`` inside its
own ``BEGIN IMMEDIATE``, so every ``GET /tracks/{id}/hot-cues`` -- which the
deck-load path issues per track -- took an exclusive transaction on the user's
rekordbox database just to look at eight slots.

The map offers a rename (``provision_and_fetch_hot_cue_slots``) or, better,
splitting the provisioning out. The provisioning turns out to be redundant
rather than merely misplaced, so it is deleted and the honest name is the one
it already had: an absent sidecar row and a row at generation 0 are the same
state and hash to the same CAS revision, and every write path provisions on
demand inside its own exclusive transaction anyway. The read now takes a
read-only connection, which makes the property structural rather than a
convention a later edit could quietly break.

Wave 4 (S8) moved this module out of ``apps/webui/server/rb_vendor_pkg/`` into
the mapped adapter root. The injected-factory design above is what makes the
move a pure relocation: the module never named a path, so leaving ``apps.webui``
took no rewiring and the ``rb_vendor`` test seams still bite. The body is
unchanged.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from apps.shared.mp3_lead_in import rekordbox_lead_in_s, to_our_ms
from apps.shared.rb_frames import msec_to_frame

from .cues import (
    HOT_CUE_SLOTS,
    _cue_revision,
    _cue_view,
    _live_slot_snapshot,
    _new_cue_id,
    _rb_timestamp,
    _require_current_revision,
    _slot_to_kind,
    _validate_cue_position,
)
from .reversal import (
    _bump_slot_generation,
    _create_reversal,
    _load_reversal,
    _read_slot_generations,
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


def _lead_in_s(
    conn: sqlite3.Connection, vendor_id: str, *, for_write: bool = False
) -> float:
    """The track's MP3 lead-in in seconds: rekordbox time minus ours.

    Same rule as ``rb_vendor_pkg.db.fetch_cues`` for a read: a track with no
    local file reads 0. A write to an MP3 whose file cannot be read refuses
    instead, since the cue would land early in rekordbox once the file is back;
    other formats have no lead-in, so 0 is their measured value.
    """
    row = conn.execute(
        "SELECT FolderPath FROM djmdContent WHERE ID = ?", (vendor_id,)
    ).fetchone()
    folder = row[0] if row else None
    lead_in_s = rekordbox_lead_in_s(folder)
    if lead_in_s is None and for_write and str(folder or "").lower().endswith(".mp3"):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "HOT_CUE_LEAD_IN_UNKNOWN",
                "message": "the MP3 is not on this machine, so its rekordbox "
                "time base cannot be read",
            },
        )
    return lead_in_s or 0.0


def _on_our_timeline(view: dict[str, Any], lead_in_s: float) -> dict[str, Any]:
    """A slot view with rekordbox's stored positions moved onto our timeline."""
    if not lead_in_s:
        return view
    out_ms = view["out_ms"]
    return {
        **view,
        "in_ms": to_our_ms(view["in_ms"], lead_in_s) if view["in_ms"] is not None else None,
        "out_ms": to_our_ms(out_ms, lead_in_s) if out_ms is not None else None,
    }


def _stored_in_ms(
    in_ms: int, preimage: Mapping[str, Any] | None, lead_in_s: float
) -> int:
    """Our ``in_ms`` in rekordbox's time, keeping the stored value when it reads as ``in_ms``.

    A cue rekordbox put inside the lead-in reads as 0, the earliest point we
    can play; putting the lead-in back would move it. Re-saving a slot at the
    position it reads (a comment or color edit, an undo, a re-save) therefore
    keeps rekordbox's own number.
    """
    if preimage is not None and preimage["in_ms"] is not None:
        stored = int(preimage["in_ms"])
        if to_our_ms(stored, lead_in_s) == in_ms:
            return stored
    return round(in_ms + lead_in_s * 1000)


def fetch_hot_cue_slots(
    vendor_id: str,
    *,
    open_ro: ConnectMaster,
) -> list[dict[str, Any]]:
    """Read all eight slot states, including revisions for empty slots.

    Read-only, and therefore takes a read-only connection: no transaction, no
    DDL, no sidecar provisioning. See the module docstring for why dropping
    the provisioning write cannot change a single returned revision.
    """
    master = open_ro()
    try:
        snapshots = {
            kind: _live_slot_snapshot(master, vendor_id, kind) for kind in _KINDS
        }
        generations = _read_slot_generations(master, vendor_id, _KINDS)
        lead_in_s = _lead_in_s(master, vendor_id)
    finally:
        master.close()
    return [
        {
            "slot": slot,
            "cue": _on_our_timeline(
                _cue_view(
                    slot,
                    snapshots[kind],
                    _cue_revision(vendor_id, kind, generations[kind], snapshots[kind]),
                ),
                lead_in_s,
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
    """CAS-save a hot cue and return its atomic preimage for one-step undo.

    ``in_ms`` is on our timeline; it is stored in rekordbox's, with the
    track's MP3 lead-in put back (``apps.shared.mp3_lead_in``).
    """
    kind = _slot_to_kind(slot)
    now = _rb_timestamp()
    master = open_rw()
    try:
        master.execute("BEGIN IMMEDIATE")
        lead_in_s = _lead_in_s(master, vendor_id, for_write=True)
        preimage = _live_slot_snapshot(master, vendor_id, kind)
        in_ms = _stored_in_ms(in_ms, preimage, lead_in_s)
        _validate_cue_position(master, vendor_id, in_ms)
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
                    msec_to_frame(in_ms),
                    color_table_index,
                    comment,
                    now,
                    preimage["id"],
                    vendor_id,
                    kind,
                ),
            )
            if update.rowcount != 1:
                raise RuntimeError("save_hot_cue: scoped cue row disappeared")  # noqa: TRY301 (rollback)
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
                    msec_to_frame(in_ms),
                    kind,
                    color_table_index,
                    comment,
                    now,
                    now,
                ),
            )
        current = _live_slot_snapshot(master, vendor_id, kind)
        if current is None:
            raise RuntimeError("save_hot_cue: committed slot disappeared")  # noqa: TRY301 (rollback)
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
        "cue": _on_our_timeline(_cue_view(slot, current, revision), lead_in_s),
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
                raise RuntimeError("clear_hot_cue: scoped cue row disappeared")  # noqa: TRY301 (rollback)
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
            raise HTTPException(  # noqa: TRY301 (rollback)
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
        lead_in_s = _lead_in_s(master, vendor_id)
        generation = _bump_slot_generation(master, vendor_id, kind)
        revision = _cue_revision(vendor_id, kind, generation, restored)
        master.commit()
    except Exception:
        master.rollback()
        raise
    finally:
        master.close()
    return {
        "cue": _on_our_timeline(_cue_view(slot, restored, revision), lead_in_s)
        if restored
        else None,
        "revision": revision,
    }


_SCALAR_TABLE = "odjAnalysisScalar"


def snapshot_content_field(
    conn: sqlite3.Connection, content_id: str, field: str
) -> dict[str, Any]:
    """Preimage for one analysis field on one djmdContent row."""
    snap: dict[str, Any] = {
        "content_id": content_id,
        "field": field,
        "key_id": None,
        "bpm": None,
        "scalar_value": None,
        "scalar_existed": False,
        "created_key_id": None,
    }
    if field == "key":
        row = conn.execute(
            "SELECT KeyID FROM djmdContent WHERE ID = ?", (content_id,)
        ).fetchone()
        if row is not None:
            snap["key_id"] = row[0]
    elif field == "bpm":
        row = conn.execute(
            "SELECT BPM FROM djmdContent WHERE ID = ?", (content_id,)
        ).fetchone()
        if row is not None:
            snap["bpm"] = row[0]
    elif field in ("loudness_lufs", "loudness_dbtp"):
        try:
            row = conn.execute(
                f"SELECT value FROM {_SCALAR_TABLE} "
                "WHERE ContentID = ? AND field = ?",
                (content_id, field),
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        if row is not None:
            snap["scalar_value"] = str(row[0])
            snap["scalar_existed"] = True
    return snap


def restore_content_field(
    conn: sqlite3.Connection, snapshot: Mapping[str, Any]
) -> None:
    """Restore one preimage. rowcount != 1 is a hard failure."""
    field = str(snapshot["field"])
    content_id = str(snapshot["content_id"])
    if field == "key":
        result = conn.execute(
            "UPDATE djmdContent SET KeyID = ? WHERE ID = ?",
            (snapshot.get("key_id"), content_id),
        )
        if result.rowcount != 1:
            raise RuntimeError(
                f"restore_content_field: KeyID restore failed for {content_id}"
            )
        return
    if field == "bpm":
        result = conn.execute(
            "UPDATE djmdContent SET BPM = ? WHERE ID = ?",
            (snapshot.get("bpm"), content_id),
        )
        if result.rowcount != 1:
            raise RuntimeError(
                f"restore_content_field: BPM restore failed for {content_id}"
            )
        return
    if field in ("loudness_lufs", "loudness_dbtp"):
        if snapshot.get("scalar_existed"):
            result = conn.execute(
                f"UPDATE {_SCALAR_TABLE} SET value = ? "
                "WHERE ContentID = ? AND field = ?",
                (snapshot.get("scalar_value"), content_id, field),
            )
            if result.rowcount != 1:
                raise RuntimeError(
                    f"restore_content_field: sidecar restore failed for {content_id}"
                )
        else:
            conn.execute(
                f"DELETE FROM {_SCALAR_TABLE} WHERE ContentID = ? AND field = ?",
                (content_id, field),
            )
        return
    raise RuntimeError(f"restore_content_field: unsupported field {field!r}")
