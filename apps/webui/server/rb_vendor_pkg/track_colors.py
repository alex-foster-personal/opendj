"""Bulk rekordbox ColorID reader for Related Tracks colour match (LIBUX-10046).

Kept apart from ``track_rows.rb_meta_from_state`` so that shared query (and
the fixtures that build djmdContent without a ColorID column) stays as is.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from apps.adapters.rekordbox import config
from apps.adapters.rekordbox.errors import _open_ro
from apps.webui.soft_deletes import has_soft_deletes

from .track_rows import _chunked


def bulk_rb_color_ids(stable_ids: Sequence[str], *, state_db_path: Path | None = None) -> dict[str, int]:
    """stable_id -> non-zero rekordbox ColorID. Missing state.db means no mappings: ``{}``."""
    ids = list(dict.fromkeys(stable_ids))
    state_path = state_db_path if state_db_path is not None else config.STATE_DB
    if not ids or not state_path.exists():
        return {}
    state = _open_ro(state_path, "STATE_DB")
    try:
        live = " AND deleted_at IS NULL" if has_soft_deletes(state, "track_vendor_ids") else ""
        sids_by_vid: dict[str, list[str]] = {}
        for chunk in _chunked(ids):
            placeholders = ",".join("?" * len(chunk))
            for sid, vid in state.execute(
                "SELECT stable_id, vendor_id FROM track_vendor_ids "
                f"WHERE vendor = 'rekordbox'{live} AND stable_id IN ({placeholders})",
                tuple(chunk),
            ):
                sids_by_vid.setdefault(str(vid), []).append(str(sid))
    finally:
        state.close()
    if not sids_by_vid:
        return {}
    master = _open_ro(config.MASTER_PLAIN_DB, "MASTER_DB")
    try:
        out: dict[str, int] = {}
        for chunk in _chunked(sorted(sids_by_vid)):
            placeholders = ",".join("?" * len(chunk))
            for vid, color_id in master.execute(
                f"SELECT ID, ColorID FROM djmdContent WHERE ID IN ({placeholders}) AND +rb_local_deleted = 0",
                tuple(chunk),
            ):
                if color_id:
                    for sid in sids_by_vid.get(str(vid), []):
                        out[sid] = int(color_id)
        return out
    finally:
        master.close()
