"""Phase-9 aux tables: DDL, row types, and queries.

Additive tables (created IF NOT EXISTS at first call) for concepts the
Phase 5 state layer doesn't own:

* ``spotify_playlist_meta`` -- per-playlist snapshot_id for idempotent
  re-imports.
* ``pending_tracks`` -- one row per unmatched Spotify track, with
  suggested purchase sources (acquisition queue).
* ``spotify_playlist_links`` -- durable ODJ (webui) playlist ↔ Spotify
  playlist link. Unlinked imports create a same-name ODJ twin.

Split out of :mod:`state_writer` (which keeps the backup / reversal /
live-write orchestration) so each module stays holdable-in-head; the
public names remain re-exported from :mod:`state_writer`.
"""
from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from apps.shared.state import db as state_db
from apps.shared.state.writer import compute_playlist_id, next_playlist_revision

__all__ = [
    "AUX_MIGRATIONS",
    "ODJ_VENDOR",
    "VENDOR",
    "PendingRow",
    "PlaylistLink",
    "already_imported_snapshot",
    "ensure_aux_tables",
    "ensure_odj_link",
    "fetch_pending_tracks",
    "fetch_playlist_link",
    "link_odj_playlist",
    "mark_pending_abandoned",
    "open_state_rw_with_aux",
]

VENDOR: str = "spotify"
ODJ_VENDOR: str = "webui"


_AUX_DDL_PLAYLIST_META = """
CREATE TABLE IF NOT EXISTS spotify_playlist_meta (
    playlist_id      TEXT PRIMARY KEY REFERENCES playlists(playlist_id)
                       ON DELETE CASCADE,
    vendor_pl_id     TEXT NOT NULL,
    snapshot_id      TEXT NOT NULL,
    track_count      INTEGER NOT NULL,
    matched_count    INTEGER NOT NULL,
    pending_count    INTEGER NOT NULL,
    last_import_at   TEXT NOT NULL
)
"""

_AUX_DDL_PENDING = """
CREATE TABLE IF NOT EXISTS pending_tracks (
    pending_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id           TEXT NOT NULL REFERENCES playlists(playlist_id)
                            ON DELETE CASCADE,
    position              INTEGER NOT NULL,
    spotify_uri           TEXT NOT NULL,
    isrc                  TEXT,
    title                 TEXT NOT NULL,
    artist                TEXT NOT NULL,
    album                 TEXT,
    duration_ms           INTEGER,
    suggested_sources_json TEXT NOT NULL,
    status                TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN
                             ('pending','purchased','resolved','abandoned')),
    added_at              TEXT NOT NULL,
    resolved_stable_id    TEXT REFERENCES tracks(stable_id) ON DELETE SET NULL,
    resolved_at           TEXT
)
"""

_AUX_IDX_PENDING_PLAYLIST = (
    "CREATE INDEX IF NOT EXISTS idx_pending_tracks_playlist "
    "ON pending_tracks(playlist_id)"
)

_AUX_IDX_PENDING_ISRC = (
    "CREATE INDEX IF NOT EXISTS idx_pending_tracks_isrc "
    "ON pending_tracks(isrc) WHERE isrc IS NOT NULL"
)

_AUX_IDX_PENDING_STATUS = (
    "CREATE INDEX IF NOT EXISTS idx_pending_tracks_status "
    "ON pending_tracks(status)"
)

_AUX_DDL_PLAYLIST_LINKS = """
CREATE TABLE IF NOT EXISTS spotify_playlist_links (
    vendor_pl_id          TEXT PRIMARY KEY,
    spotify_playlist_id   TEXT NOT NULL REFERENCES playlists(playlist_id)
                            ON DELETE CASCADE,
    odj_playlist_id       TEXT NOT NULL REFERENCES playlists(playlist_id)
                            ON DELETE CASCADE,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
)
"""

_AUX_IDX_PLAYLIST_LINKS_ODJ = (
    "CREATE INDEX IF NOT EXISTS idx_spotify_playlist_links_odj "
    "ON spotify_playlist_links(odj_playlist_id)"
)

AUX_MIGRATIONS: tuple[str, ...] = (
    _AUX_DDL_PLAYLIST_META,
    _AUX_DDL_PENDING,
    _AUX_IDX_PENDING_PLAYLIST,
    _AUX_IDX_PENDING_ISRC,
    _AUX_IDX_PENDING_STATUS,
    _AUX_DDL_PLAYLIST_LINKS,
    _AUX_IDX_PLAYLIST_LINKS_ODJ,
)


def ensure_aux_tables(conn: sqlite3.Connection) -> None:
    """Create Phase-9 tables + indexes if missing. Idempotent."""
    for stmt in AUX_MIGRATIONS:
        conn.execute(stmt)


def open_state_rw_with_aux(path: Path | None = None) -> sqlite3.Connection:
    """Open the state DB rw and ensure Phase-9 aux tables exist."""
    conn = state_db.open_rw(path)
    ensure_aux_tables(conn)
    return conn


def already_imported_snapshot(
    conn: sqlite3.Connection,
    vendor_pl_id: str,
) -> str | None:
    """Return the stored snapshot_id for this Spotify playlist, or None."""
    ensure_aux_tables(conn)
    row = conn.execute(
        "SELECT snapshot_id FROM spotify_playlist_meta WHERE vendor_pl_id = ?",
        (vendor_pl_id,),
    ).fetchone()
    return row[0] if row else None


# ----- ODJ playlist links --------------------------------------------------


@dataclass(frozen=True)
class PlaylistLink:
    vendor_pl_id: str
    spotify_playlist_id: str
    odj_playlist_id: str
    created_at: str
    updated_at: str


def fetch_playlist_link(
    conn: sqlite3.Connection,
    vendor_pl_id: str,
) -> PlaylistLink | None:
    """Return the ODJ↔Spotify link for ``vendor_pl_id``, or None."""
    ensure_aux_tables(conn)
    row = conn.execute(
        "SELECT vendor_pl_id, spotify_playlist_id, odj_playlist_id, "
        "created_at, updated_at FROM spotify_playlist_links "
        "WHERE vendor_pl_id = ?",
        (vendor_pl_id,),
    ).fetchone()
    if row is None:
        return None
    return PlaylistLink(*row)


def link_odj_playlist(
    conn: sqlite3.Connection,
    *,
    vendor_pl_id: str,
    spotify_playlist_id: str,
    odj_playlist_id: str,
    now: str | None = None,
) -> PlaylistLink:
    """Register / refresh an ODJ playlist link for a Spotify playlist."""
    ensure_aux_tables(conn)
    ts = now or datetime.now(UTC).isoformat()
    existing = fetch_playlist_link(conn, vendor_pl_id)
    if existing is not None and existing.odj_playlist_id == odj_playlist_id:
        conn.execute(
            "UPDATE spotify_playlist_links SET updated_at = ?, "
            "spotify_playlist_id = ? WHERE vendor_pl_id = ?",
            (ts, spotify_playlist_id, vendor_pl_id),
        )
        return PlaylistLink(
            vendor_pl_id=vendor_pl_id,
            spotify_playlist_id=spotify_playlist_id,
            odj_playlist_id=odj_playlist_id,
            created_at=existing.created_at,
            updated_at=ts,
        )
    conn.execute(
        """
        INSERT INTO spotify_playlist_links
          (vendor_pl_id, spotify_playlist_id, odj_playlist_id,
           created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(vendor_pl_id) DO UPDATE SET
          spotify_playlist_id = excluded.spotify_playlist_id,
          odj_playlist_id = excluded.odj_playlist_id,
          updated_at = excluded.updated_at
        """,
        (vendor_pl_id, spotify_playlist_id, odj_playlist_id, ts, ts),
    )
    return PlaylistLink(
        vendor_pl_id=vendor_pl_id,
        spotify_playlist_id=spotify_playlist_id,
        odj_playlist_id=odj_playlist_id,
        created_at=ts if existing is None else existing.created_at,
        updated_at=ts,
    )


def ensure_odj_link(
    conn: sqlite3.Connection,
    *,
    vendor_pl_id: str,
    spotify_playlist_id: str,
    playlist_name: str,
    now: str | None = None,
) -> tuple[PlaylistLink, bool]:
    """Return existing ODJ link, or create a same-name webui playlist + link.

    Returns ``(link, created)`` where ``created`` is True only when a new
    ODJ playlist row was inserted.
    """
    ensure_aux_tables(conn)
    ts = now or datetime.now(UTC).isoformat()
    existing = fetch_playlist_link(conn, vendor_pl_id)
    if existing is not None:
        # Confirm the ODJ playlist still exists; recreate if deleted.
        still = conn.execute(
            "SELECT 1 FROM playlists WHERE playlist_id = ?",
            (existing.odj_playlist_id,),
        ).fetchone()
        if still is not None:
            refreshed = link_odj_playlist(
                conn,
                vendor_pl_id=vendor_pl_id,
                spotify_playlist_id=spotify_playlist_id,
                odj_playlist_id=existing.odj_playlist_id,
                now=ts,
            )
            return refreshed, False

    vendor_pl_uuid = uuid.uuid4().hex
    odj_id = compute_playlist_id(ODJ_VENDOR, vendor_pl_uuid)
    name = (playlist_name or "").strip() or f"Spotify {vendor_pl_id}"
    revision = next_playlist_revision(conn, odj_id, ts)
    conn.execute(
        """
        INSERT INTO playlists
          (playlist_id, name, vendor, vendor_pl_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (odj_id, name, ODJ_VENDOR, vendor_pl_uuid, ts, revision),
    )
    link = link_odj_playlist(
        conn,
        vendor_pl_id=vendor_pl_id,
        spotify_playlist_id=spotify_playlist_id,
        odj_playlist_id=odj_id,
        now=ts,
    )
    return link, True


# ----- pending tracks ------------------------------------------------------


@dataclass(frozen=True)
class PendingRow:
    pending_id: int
    playlist_id: str
    position: int
    spotify_uri: str
    isrc: str | None
    title: str
    artist: str
    album: str | None
    duration_ms: int | None
    suggested_sources_json: str
    status: str
    added_at: str
    resolved_stable_id: str | None
    resolved_at: str | None


def fetch_pending_tracks(
    conn: sqlite3.Connection,
    playlist_id: str,
    *,
    status: str | None = "pending",
) -> list[PendingRow]:
    """Return pending rows for ``playlist_id`` filtered by ``status``."""
    ensure_aux_tables(conn)
    query = (
        "SELECT pending_id, playlist_id, position, spotify_uri, isrc, title, "
        "artist, album, duration_ms, suggested_sources_json, status, added_at, "
        "resolved_stable_id, resolved_at FROM pending_tracks "
        "WHERE playlist_id = ?"
    )
    params: list[object] = [playlist_id]
    if status is not None:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY position"
    rows = conn.execute(query, params).fetchall()
    return [PendingRow(*r) for r in rows]


def mark_pending_abandoned(
    conn: sqlite3.Connection,
    pending_ids: Iterable[int],
) -> int:
    """Mark pending rows as ``abandoned``. Returns row count touched."""
    ensure_aux_tables(conn)
    now = datetime.now(UTC).isoformat()
    ids = list(pending_ids)
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    cur = conn.execute(
        f"UPDATE pending_tracks SET status = 'abandoned', resolved_at = ? "
        f"WHERE pending_id IN ({placeholders}) AND status != 'abandoned'",
        [now, *ids],
    )
    return cur.rowcount or 0
