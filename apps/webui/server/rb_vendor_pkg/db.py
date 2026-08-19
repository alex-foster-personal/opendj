"""Read-only master.plain.db access: playlist order + cue reads.

Moved verbatim from ``apps/webui/server/rb_vendor.py`` C4 (lines 845-948 on
``af--t4-design``), per ``.planning/t3b-decomposition-map.md`` target #9
(``adapters/rekordbox/db.py``, budget 130 lines). rb_vendor.py re-exports
these three names so the 10 route importers and the pinning test suite
(``tests/webui/test_rb_vendor_cache.py``, ``tests/webui/conftest.py``'s
``_stub_rb_vendor`` fixture) see no behavior change.

Config constants (``MASTER_PLAIN_DB``) and the sibling cluster function
``_cue_snapshot_from_row`` (C10, not part of this slice) are looked up as
``rb_vendor.<name>`` INSIDE each function body rather than imported by value
at module load time. This is required, not stylistic: ``MASTER_PLAIN_DB`` is
a module-level rebindable attribute that both
``.planning/e2e-gating/run_daemon.py`` and the test suite monkeypatch on the
``rb_vendor`` module object directly (see the decomposition map section 2,
target #2 note); a `from rb_vendor import MASTER_PLAIN_DB` here would
capture a stale value at import time and silently stop honoring those
monkeypatches. Importing the module object (not its attributes) also avoids
a circular-import deadlock at package load: rb_vendor.py imports this module
for the re-export, and this module imports the (partially-initialized)
rb_vendor module back, which Python resolves fine as long as no module-level
code here dereferences an rb_vendor attribute before both modules finish
loading.

``_open_ro`` is duplicated (not imported) from rb_vendor.py's C1 cluster:
the decomposition map's target design lists it as living in BOTH
``paths.py`` (S0's slice) and ``db.py`` (this slice, target #9 explicitly
names it as one of db.py's contents), so this duplication is intentional
and matches the target split, not an oversight. Dedup is S8's job once S0's
``paths.py`` exists to be the one owner.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from apps.webui.server import rb_vendor


def _open_ro(path: Path, label: str) -> sqlite3.Connection:
    if not path.exists():
        raise HTTPException(
            status_code=500,
            detail={
                "code": f"{label}_UNAVAILABLE",
                "message": f"required database missing on disk: {path}",
            },
        )
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


# ----- playlist ordering (djmdPlaylist Seq) -----------------------------------


def playlist_order_index() -> dict[str, int]:
    """djmdPlaylist ID -> flattened rekordbox tree position (0-based).

    Rekordbox orders the playlist tree by (ParentID, Seq) - a user-managed
    custom order, NOT alphabetical (SCREENSHOT-SPEC 5b). The flat /performance
    tree needs one comparable number per playlist, so the tree is walked
    depth-first from 'root' with siblings ordered by Seq; the visit order is
    the index. Folders are included (they carry Seq too and may map to
    playlists elsewhere); unknown parents simply never get visited and their
    subtrees stay absent from the map - a real data state the caller must
    treat as 'no rekordbox order known'.
    """
    master = _open_ro(rb_vendor.MASTER_PLAIN_DB, "MASTER_DB")
    try:
        rows = master.execute(
            "SELECT ID, ParentID, Seq FROM djmdPlaylist WHERE rb_local_deleted = 0"
        ).fetchall()
    finally:
        master.close()

    children: dict[str, list[tuple[int, str]]] = {}
    for pl_id, parent_id, seq in rows:
        children.setdefault(str(parent_id), []).append(
            (int(seq) if seq is not None else 0, str(pl_id))
        )
    order: dict[str, int] = {}
    stack: list[str] = [
        pl_id for _, pl_id in sorted(children.get("root", []), reverse=True)
    ]
    while stack:
        pl_id = stack.pop()
        order[pl_id] = len(order)
        stack.extend(c for _, c in sorted(children.get(pl_id, []), reverse=True))
    return order


# ----- cues (djmdCue, NOT ANLZ) ----------------------------------------------


def fetch_cues(vendor_id: str) -> list[dict[str, Any]]:
    """Live djmdCue rows mapped to the COMPONENT-MAP 2.3 cue shape."""
    master = _open_ro(rb_vendor.MASTER_PLAIN_DB, "MASTER_DB")
    try:
        rows = master.execute(
            "SELECT ID, Kind, InMsec, InFrame, InMpegFrame, InMpegAbs, "
            "       OutMsec, OutFrame, ActiveLoop, BeatLoopSize, "
            "       ColorTableIndex, Comment, updated_at "
            "FROM djmdCue WHERE ContentID = ? AND rb_local_deleted = 0",
            (vendor_id,),
        ).fetchall()
    finally:
        master.close()

    cues: list[dict[str, Any]] = []
    for row in rows:
        snapshot = rb_vendor._cue_snapshot_from_row(row)
        kind_i = snapshot["kind"]
        if kind_i is None or not 0 <= int(kind_i) <= 8:
            continue  # Kind 9-11 excluded v1: slot mapping unverified (PARITY-TODO)
        in_ms = snapshot["in_ms"]
        out_ms = snapshot["out_ms"]
        active_loop = snapshot["active_loop"]
        loop_size = snapshot["beat_loop_size"]
        color_idx = snapshot["color_table_index"]
        comment = snapshot["comment"]
        is_loop = bool(out_ms and int(out_ms) > 0)
        if int(kind_i) == 0:
            kind = "loop" if is_loop else "memory"
            slot: str | None = None
        else:
            kind = "hot_cue"
            slot = rb_vendor.HOT_CUE_SLOTS[int(kind_i) - 1]
        cues.append(
            {
                "kind": kind,
                "slot": slot,
                "in_ms": int(in_ms) if in_ms is not None else None,
                "out_ms": int(out_ms) if is_loop else None,
                "is_loop": is_loop,
                "active_loop": bool(active_loop),
                "beat_loop_size": int(loop_size) if loop_size is not None else None,
                "color_table_index": int(color_idx) if color_idx is not None else None,
                "comment": comment or None,
            }
        )
    cues.sort(key=lambda c: c["in_ms"] if c["in_ms"] is not None else -1)
    return cues


def count_cues(vendor_id: str) -> int:
    """All live djmdCue rows for the track (incl. Kind 9-11, for rb-meta)."""
    master = _open_ro(rb_vendor.MASTER_PLAIN_DB, "MASTER_DB")
    try:
        row = master.execute(
            "SELECT COUNT(*) FROM djmdCue WHERE ContentID = ? AND rb_local_deleted = 0",
            (vendor_id,),
        ).fetchone()
        return int(row[0])
    finally:
        master.close()
