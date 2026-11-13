"""Library-wide rekordbox loop cues for the edge sampler, read-only.

Spec: ``(private path)`` section 3.2 (slots come from loop hot cues and memory loops).
One read of ``djmdCue`` for the whole library, converted exactly as the hot-cue reader converts
one track's cues (``_cue_snapshot_from_row``, the same Kind to slot map, the same MP3 lead-in
shift onto our timeline), then mapped to stable ids through ``track_vendor_ids``, the mapping
``resolve_content`` reads. Cached per process by the master DB's path, size and mtime. The
database is opened read-only and never written.

Having no rekordbox is a real state, not an error: the catalogue still lists the library's own
loops and reports why rekordbox added none (``RbLoops.status``). Anything else that goes wrong
reading a database that is there (a missing table, a locked file) raises.

Mini-PRD:
  ✔︎ ✅ 🎯 every rekordbox loop (hot A..H and memory) of the library in one read, never written
    [if] loops are read [then] the master DB file's bytes are unchanged [else ⛔️]
    [if] a cue is a point cue, deleted, or of an unmapped Kind [then] it is no loop [else ⛔️]
  ✔︎ ✅ 🎯 no rekordbox database is the state ``absent``, an unreadable one ``unreadable``
    [if] MASTER_PLAIN_DB does not exist [then] state absent, no loops, status names it [else ⛔️]
    [if] the file is not an sqlite database [then] state unreadable with sqlite's reason [else ⛔️]
  ✔︎ ✅ 🎯 loops map to live library tracks through track_vendor_ids
    [if] the vendor mapping or the track is soft-deleted [then] its loops are not listed [else ⛔️]
  ✔︎ ✅ 🎯 the read is cached until the database file changes
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal

from apps.adapters.rekordbox import config as rb_config
from apps.adapters.rekordbox.cue_kind import MEMORY_KIND, is_mapped_kind, kind_to_slot
from apps.adapters.rekordbox.cues import _cue_snapshot_from_row
from apps.adapters.rekordbox.errors import _open_ro
from apps.shared.mp3_lead_in import rekordbox_lead_in_s, to_our_ms

RekordboxState = Literal["ok", "absent", "unreadable"]

_LOOP_CUES_SQL = (
    "SELECT c.ContentID, t.FolderPath, c.ID, c.Kind, c.InMsec, c.InFrame, c.InMpegFrame, c.InMpegAbs, "
    "       c.OutMsec, c.OutFrame, c.ActiveLoop, c.BeatLoopSize, c.ColorTableIndex, c.Comment, c.updated_at "
    "FROM djmdCue c JOIN djmdContent t ON t.ID = c.ContentID "
    "WHERE c.rb_local_deleted = 0 AND c.OutMsec > 0 "
    "ORDER BY c.ContentID, c.InMsec, c.Kind"
)
_VENDOR_MAP_SQL = (
    "SELECT v.stable_id, v.vendor_id FROM track_vendor_ids v JOIN tracks t ON t.stable_id = v.stable_id "
    "WHERE v.vendor = 'rekordbox' AND v.deleted_at IS NULL AND t.deleted_at IS NULL"
)


@dataclass(frozen=True)
class RbLoop:
    """One rekordbox loop on our timeline: a hot loop's bank letter, or None for a memory loop."""

    slot: str | None
    in_ms: int
    out_ms: int
    color: int | None = None  # the cue's ColorTableIndex; None when rekordbox stores none


@dataclass(frozen=True)
class RbLoops:
    """Every rekordbox loop by rekordbox content id, and whether rekordbox could be read at all."""

    state: RekordboxState
    by_vendor_id: Mapping[str, tuple[RbLoop, ...]]
    reason: str | None = None  # why there are no rekordbox loops; None exactly when state is "ok"

    @property
    def status(self) -> str:
        """``ok``, or ``unavailable: <reason>``: what the slots page reports."""
        return "ok" if self.state == "ok" else f"unavailable: {self.reason}"


class _Cache:
    key: ClassVar[tuple[str, int, int] | None] = None
    value: ClassVar[RbLoops | None] = None


def reset_cache() -> None:
    """Forget the cached read, so the next ``loop_cues`` reads the database again."""
    _Cache.key, _Cache.value = None, None


# ---------- read ---------------------------------------------------------------------


def _loop_of(snap: Mapping[str, Any], lead_in_s: float) -> RbLoop | None:
    kind, in_ms, out_ms = snap["kind"], snap["in_ms"], snap["out_ms"]
    if kind is None or in_ms is None or out_ms is None or not is_mapped_kind(kind):
        return None
    slot = None if kind == MEMORY_KIND else kind_to_slot(kind)
    if kind != MEMORY_KIND and slot is None:
        return None
    a, b = to_our_ms(in_ms, lead_in_s), to_our_ms(out_ms, lead_in_s)
    return RbLoop(slot, a, b, snap["color_table_index"]) if b > a else None


def _read(path: Path) -> RbLoops:
    master = _open_ro(path, "MASTER_DB")
    try:
        rows = master.execute(_LOOP_CUES_SQL).fetchall()
    finally:
        master.close()
    # A reader that serves positions may take "no local file" as no lead-in (rekordbox_lead_in_s).
    lead_in: dict[str, float] = {}
    out: dict[str, list[RbLoop]] = {}
    for content_id, folder, *cue_row in rows:
        snap = _cue_snapshot_from_row(tuple(cue_row))
        vid = str(content_id)
        if vid not in lead_in:
            lead_in[vid] = rekordbox_lead_in_s(folder) or 0.0
        loop = _loop_of(snap, lead_in[vid])
        if loop is not None:
            out.setdefault(vid, []).append(loop)
    return RbLoops("ok", {vid: tuple(loops) for vid, loops in out.items()})


def loop_cues() -> RbLoops:
    """Every rekordbox loop (hot and memory) by rekordbox content id, cached by DB size and mtime."""
    path = Path(rb_config.MASTER_PLAIN_DB)
    try:
        st = path.stat()
    except FileNotFoundError:
        return RbLoops("absent", {}, f"rekordbox database not found at {path}")
    key = (str(path), st.st_size, st.st_mtime_ns)
    if _Cache.key == key and _Cache.value is not None:
        return _Cache.value
    try:
        value = _read(path)
    except sqlite3.OperationalError:
        raise  # the file is a database we failed to query: a real failure, not a missing rekordbox
    except sqlite3.DatabaseError as exc:
        return RbLoops("unreadable", {}, f"rekordbox database {path.name} is not a readable sqlite file: {exc}")
    _Cache.key, _Cache.value = key, value
    return value


# ---------- map to library tracks ----------------------------------------------------


def vendor_map(conn: sqlite3.Connection) -> dict[str, str]:
    """rekordbox content id to live stable_id, over live mappings of live tracks."""
    return {vid: sid for sid, vid in conn.execute(_VENDOR_MAP_SQL)}


def loops_by_stable_id(conn: sqlite3.Connection, loops: RbLoops) -> dict[str, tuple[RbLoop, ...]]:
    """The rekordbox loops of live library tracks, by stable_id (empty when rekordbox was not read)."""
    if loops.state != "ok":
        return {}
    vendor = vendor_map(conn)
    return {vendor[vid]: rb for vid, rb in loops.by_vendor_id.items() if vid in vendor}
