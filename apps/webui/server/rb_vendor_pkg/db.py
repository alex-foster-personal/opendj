"""Read-only master.plain.db access: playlist order + cue reads.

Moved verbatim from ``apps/webui/server/rb_vendor.py`` C4 (lines 845-948 on
``af--t4-design``), per ``.planning/t3b-decomposition-map.md`` target #9
(``adapters/rekordbox/db.py``, budget 130 lines). rb_vendor.py re-exports
these three names so the 10 route importers and the pinning test suite
(``tests/webui/test_rb_vendor_cache.py``, ``tests/webui/conftest.py``'s
``_stub_rb_vendor`` fixture) see no behavior change.

``MASTER_PLAIN_DB`` is read as ``config.MASTER_PLAIN_DB`` inside each
function body, never imported by value. That is required, not stylistic: it
is a rebindable override both ``.planning/e2e-gating/run_daemon.py`` and the
test suite assign to (map section 2, target #2), and a
``from .config import MASTER_PLAIN_DB`` here would capture the value at
import time and silently stop honoring them. Until wave 4 the same lookup
went through ``rb_vendor``, which also had to be deferred to dodge a
circular import; that cycle is gone -- ``_cue_snapshot_from_row`` and
``HOT_CUE_SLOTS`` are now plain top-level imports from
``apps/adapters/rekordbox/cues.py``.

``_open_ro`` was duplicated here rather than imported, because the map lists
it under BOTH target #4 (paths.py) and target #9 (db.py) and neither existed
when C4 moved. Wave 4 deleted the copy: the one definition lives in
``apps/adapters/rekordbox/errors.py`` and this module imports it.
"""
from __future__ import annotations

from typing import Any

from apps.adapters.rekordbox import config
from apps.shared.mp3_lead_in import rekordbox_lead_in_s, to_our_ms
from apps.adapters.rekordbox.cues import HOT_CUE_SLOTS, _cue_snapshot_from_row
from apps.adapters.rekordbox.errors import _open_ro

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
    master = _open_ro(config.MASTER_PLAIN_DB, "MASTER_DB")
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
    """Live djmdCue rows mapped to the COMPONENT-MAP 2.3 cue shape, on OUR timeline.

    rekordbox stores positions from the first sample of a raw decode; our
    decoders trim an MP3's encoder lead-in, so every position here has that
    lead-in taken off (``apps.shared.mp3_lead_in``). A track with no local file
    has no lead-in to read and cannot play here, so it is served as stored.
    """
    master = _open_ro(config.MASTER_PLAIN_DB, "MASTER_DB")
    try:
        folder = master.execute(
            "SELECT FolderPath FROM djmdContent WHERE ID = ?", (vendor_id,)
        ).fetchone()
        rows = master.execute(
            "SELECT ID, Kind, InMsec, InFrame, InMpegFrame, InMpegAbs, "
            "       OutMsec, OutFrame, ActiveLoop, BeatLoopSize, "
            "       ColorTableIndex, Comment, updated_at "
            "FROM djmdCue WHERE ContentID = ? AND rb_local_deleted = 0",
            (vendor_id,),
        ).fetchall()
    finally:
        master.close()

    lead_in_s = rekordbox_lead_in_s(folder[0] if folder else None) or 0.0
    cues: list[dict[str, Any]] = []
    for row in rows:
        snapshot = _cue_snapshot_from_row(row)
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
            slot = HOT_CUE_SLOTS[int(kind_i) - 1]
        cues.append(
            {
                "kind": kind,
                "slot": slot,
                "in_ms": to_our_ms(int(in_ms), lead_in_s) if in_ms is not None else None,
                "out_ms": to_our_ms(int(out_ms), lead_in_s) if is_loop else None,
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
    master = _open_ro(config.MASTER_PLAIN_DB, "MASTER_DB")
    try:
        row = master.execute(
            "SELECT COUNT(*) FROM djmdCue WHERE ContentID = ? AND rb_local_deleted = 0",
            (vendor_id,),
        ).fetchone()
        return int(row[0])
    finally:
        master.close()
