"""djay playlist writer wrapper (Phase 3 <-> Phase 8 bridge).

Wraps Phase 3's ``apps.sync.playlist_apply._apply_single_op`` so the Phase 8
:class:`apps.smartlists.writers.PlaylistWriter` protocol can drive per-
smartlist ``create`` / ``update`` ops against djay's ``MediaLibrary.db``.

Design:
  * ``playlist_exists`` reads the canonical name from ``database2`` where
    collection='mediaItemPlaylists'.
  * ``create_playlist`` emits a Phase 3 plan op ("create") and applies it.
  * ``apply_diff`` resolves current membership -> target-after-diff and
    emits an "update" op.
  * stable_id -> djay_uuid resolution goes through the state-layer
    ``track_vendor_ids`` table (vendor="djay").
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _resolve_djay_uuid(
    state_conn: sqlite3.Connection, stable_id: str
) -> str | None:
    """Return the djay UUID for ``stable_id`` or None."""
    row = state_conn.execute(
        "SELECT vendor_id FROM track_vendor_ids "
        "WHERE stable_id = ? AND vendor = 'djay' LIMIT 1",
        (stable_id,),
    ).fetchone()
    return row[0] if row else None


def _find_djay_playlist(
    djay_conn: sqlite3.Connection, name: str
) -> tuple[str, list[int]] | None:
    """Find a djay playlist by name. Returns ``(uuid, rowids)`` or None.

    Naive name match: scans ``mediaItemPlaylists`` rows for a blob that
    contains the name bytes. Good enough for the marker-prefix names the
    Phase 8 materialiser uses (unique per smartlist).
    """
    name_bytes = name.encode("utf-8")
    rows = djay_conn.execute(
        "SELECT key, data FROM database2 "
        "WHERE collection = 'mediaItemPlaylists'"
    ).fetchall()
    uuid: str | None = None
    for key, blob in rows:
        if blob is None:
            continue
        if name_bytes in bytes(blob):
            uuid = key
            break
    if uuid is None:
        return None
    pages = djay_conn.execute(
        'SELECT data FROM view_mediaItemPlaylistView_page WHERE "group" = ?',
        (uuid,),
    ).fetchall()
    from apps.sync import playlist_tsaf as ptsaf

    rowids: list[int] = []
    for (blob,) in pages:
        rowids.extend(ptsaf.parse_page_data(blob))
    return uuid, rowids


@dataclass
class DjayPlaylistWriter:
    """``PlaylistWriter`` that drives Phase 3 ops against djay's DB.

    Attributes
    ----------
    djay_db_path
        Path to the djay ``MediaLibrary.db`` (live or staged copy).
    state_conn
        SQLite connection to the shared state DB (for stable_id ->
        djay_uuid resolution via ``track_vendor_ids``).
    vendor
        Vendor tag surfaced in ``MaterializeResult.writers_applied``.
    leaf_type_byte
        Passed through to ``playlist_apply._apply_single_op``. None uses
        the Phase 3 default.
    """

    djay_db_path: Path
    state_conn: sqlite3.Connection
    vendor: str = "djay"
    leaf_type_byte: int | None = None

    def _open_djay(self) -> sqlite3.Connection:
        return sqlite3.connect(
            f"file:{self.djay_db_path}?mode=rwc", uri=True,
            isolation_level=None,
        )

    def playlist_exists(self, name: str) -> bool:
        con = self._open_djay()
        try:
            return _find_djay_playlist(con, name) is not None
        finally:
            con.close()

    def _resolve_or_raise(self, stable_ids: list[str]) -> list[str]:
        uuids: list[str] = []
        missing: list[str] = []
        for sid in stable_ids:
            u = _resolve_djay_uuid(self.state_conn, sid)
            if u is None:
                missing.append(sid)
            else:
                uuids.append(u)
        if missing:
            raise RuntimeError(
                f"djay: no djay_uuid mapping for stable_ids "
                f"{missing[:5]}{'...' if len(missing) > 5 else ''}"
            )
        return uuids

    def _apply_op(self, op: dict) -> None:
        from apps.sync.playlist_apply import (
            _apply_single_op, PlaylistApplyError,
        )

        con = self._open_djay()
        try:
            con.execute("BEGIN IMMEDIATE")
            try:
                result = _apply_single_op(
                    con, op, leaf_type_byte=self.leaf_type_byte
                )
                if result.status == "failed":
                    con.execute("ROLLBACK")
                    raise PlaylistApplyError(result.message)
                con.execute("COMMIT")
            except PlaylistApplyError:
                raise
            except Exception:
                try:
                    con.execute("ROLLBACK")
                except sqlite3.DatabaseError:
                    pass
                raise
        finally:
            con.close()

    def create_playlist(self, name: str, track_ids: list[str]) -> None:
        uuids = self._resolve_or_raise(list(track_ids))
        op = {
            "rb_id": "",
            "rb_name": name,
            "op": "create",
            "djay_uuid": None,
            "target_members": [{"djay_uuid": u} for u in uuids],
        }
        self._apply_op(op)

    def apply_diff(
        self, name: str, added: list[str], removed: list[str],
    ) -> None:
        con = self._open_djay()
        try:
            found = _find_djay_playlist(con, name)
            if found is None:
                raise RuntimeError(
                    f"djay: playlist {name!r} not found; cannot apply diff"
                )
            uuid, current_rowids = found
            row_to_uuid = {
                int(rowid): key
                for rowid, key in con.execute(
                    "SELECT rowid, key FROM database2 "
                    "WHERE collection = 'mediaItemUserData'"
                )
            }
            current_uuids = [row_to_uuid.get(r, "") for r in current_rowids]
        finally:
            con.close()

        add_uuids = self._resolve_or_raise(list(added))
        remove_uuids = self._resolve_or_raise(list(removed))
        remove_set = set(remove_uuids)
        target = [u for u in current_uuids if u and u not in remove_set]
        target.extend(add_uuids)

        op = {
            "rb_id": "",
            "rb_name": name,
            "op": "update",
            "djay_uuid": uuid,
            "target_members": [{"djay_uuid": u} for u in target],
        }
        self._apply_op(op)


def build_djay_writer(
    djay_db_path: Path | None = None,
    state_conn: sqlite3.Connection | None = None,
) -> DjayPlaylistWriter | None:
    """Construct a :class:`DjayPlaylistWriter` or return None on failure."""
    if djay_db_path is None:
        try:
            from apps.shared import paths

            djay_db_path = getattr(paths, "DJAY_WORKING_DB", None)
        except Exception:
            return None
    if not djay_db_path or not Path(djay_db_path).exists():
        return None
    if state_conn is None:
        try:
            from apps.shared.state.db import open_ro as _open_state_ro

            state_conn = _open_state_ro()
        except Exception:
            return None
    return DjayPlaylistWriter(
        djay_db_path=Path(djay_db_path), state_conn=state_conn
    )


__all__ = [
    "DjayPlaylistWriter",
    "build_djay_writer",
    "_resolve_djay_uuid",
    "_find_djay_playlist",
]
