"""Derived Inbox playlist (_incoming), LIBM-D1 / ADR-0019."""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.webui.server.backend import Playlist, Track

INBOX_PLAYLIST_ID = "_incoming"
INBOX_PLAYLIST_NAME = "Inbox"
INBOX_VENDOR = "system"
INBOX_STABLE_TS = "1970-01-01T00:00:00.000000Z"

_UNFILED_SQL = """
SELECT t.stable_id, t.updated_at
FROM tracks t
WHERE t.deleted_at IS NULL
  AND NOT EXISTS (
    SELECT 1
    FROM playlist_memberships m
    JOIN playlists p ON p.playlist_id = m.playlist_id
    WHERE m.stable_id = t.stable_id
      AND m.deleted_at IS NULL
      AND p.deleted_at IS NULL
  )
ORDER BY t.created_at, t.stable_id
"""


def inbox_updated_at(member_updated_at: list[str]) -> str:
    return max(member_updated_at) if member_updated_at else INBOX_STABLE_TS


def make_inbox(*, items: list[str], updated_at: str | None = None) -> Playlist:
    from apps.webui.server.backend import Playlist

    ts = updated_at if updated_at is not None else INBOX_STABLE_TS
    return Playlist(
        playlist_id=INBOX_PLAYLIST_ID,
        name=INBOX_PLAYLIST_NAME,
        vendor=INBOX_VENDOR,
        vendor_pl_id=None,
        items=list(items),
        item_ids=[],
        created_at=INBOX_STABLE_TS,
        updated_at=ts,
        forbid_duplicates=False,
    )


def with_inbox(
    stored: list[Playlist],
    items: list[str],
    *,
    updated_at: str | None = None,
) -> list[Playlist]:
    """Prepend Inbox. Drop any stored row whose playlist_id is _incoming."""
    live = [p for p in stored if p.playlist_id != INBOX_PLAYLIST_ID]
    return [make_inbox(items=items, updated_at=updated_at), *live]


def unfiled_ids_in_memory(
    tracks: Mapping[str, Track],
    playlists: Mapping[str, Playlist],
) -> list[str]:
    """Live in-memory tracks whose stable_id is in no Playlist.items."""
    filed: set[str] = set()
    for pl in playlists.values():
        filed.update(pl.items)
    unfiled = [sid for sid in tracks if sid not in filed]
    unfiled.sort(key=lambda sid: (tracks[sid].created_at, sid))
    return unfiled


def unfiled_ids_from_conn(conn: sqlite3.Connection) -> list[str]:
    """Live tracks with no live membership of a live playlist."""
    if not _table_exists(conn, "tracks"):
        return []
    has_memberships = _table_exists(conn, "playlist_memberships")
    has_playlists = _table_exists(conn, "playlists")
    if not has_memberships or not has_playlists:
        rows = conn.execute(
            "SELECT stable_id FROM tracks WHERE deleted_at IS NULL "
            "ORDER BY created_at, stable_id"
        ).fetchall()
        return [r[0] for r in rows]
    rows = conn.execute(_UNFILED_SQL).fetchall()
    return [r[0] for r in rows]


def inbox_updated_at_from_conn(
    conn: sqlite3.Connection,
    unfiled: list[str],
) -> str:
    if not unfiled:
        return INBOX_STABLE_TS
    placeholders = ",".join("?" * len(unfiled))
    row = conn.execute(
        f"SELECT MAX(updated_at) FROM tracks WHERE stable_id IN ({placeholders})",
        unfiled,
    ).fetchone()
    val = row[0] if row is not None else None
    return val if val else INBOX_STABLE_TS


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None
