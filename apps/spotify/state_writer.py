"""Shared-state writes for Phase 9 Spotify imports.

Phase 5 state layer has ``playlists(vendor, vendor_pl_id, ...)``
already. Phase 9 adds additive tables (created IF NOT EXISTS at
first call) for concepts Phase 5 doesn't own:

* ``spotify_playlist_meta`` -- per-playlist snapshot_id for idempotent
  re-imports.
* ``pending_tracks`` -- one row per unmatched Spotify track, with
  suggested purchase sources (acquisition queue).
* ``spotify_playlist_links`` -- durable ODJ (webui) playlist ↔ Spotify
  playlist link. Unlinked imports create a same-name ODJ twin.

Live writes also:

* set ``track_vendor_ids`` (vendor=spotify) for matched local tracks so
  re-imports do not duplicate identity;
* upsert synthetic ``tracks`` rows for unmatched Spotify tracks
  (``file_path = spotify:track:...``) and put them on both the Spotify
  vendor playlist and the linked ODJ twin so the browser can render
  lightly-green pending rows inline.

Every live write:
    1. backup the state DB via the SQLite ``.backup`` API.
    2. write a self-contained reversal script next to the backup.
    3. run the inserts in one transaction; rollback on any exception.
    4. upsert the adapter registry row.

Dry-run mode never reaches ``write_playlist_and_pending``; the
importer emits artifacts only. See CONTEXT D6.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import quote_plus

from apps.shared.state import db as state_db
from apps.shared.state import paths as state_paths
from apps.shared.state.writer import compute_playlist_id, next_playlist_revision

from .client import SpotifyPlaylist, SpotifyTrack
from .matcher_adapter import MatchResult

__all__ = [
    "AUX_MIGRATIONS",
    "WriteSummary",
    "PendingRow",
    "PlaylistLink",
    "ensure_aux_tables",
    "backup_state_db",
    "emit_reversal_script",
    "write_playlist_and_pending",
    "already_imported_snapshot",
    "fetch_pending_tracks",
    "fetch_playlist_link",
    "link_odj_playlist",
    "ensure_odj_link",
    "synthetic_stable_id",
    "mark_pending_abandoned",
    "open_state_rw_with_aux",
    "SUGGESTED_SOURCE_KEYS",
    "VENDOR",
    "ODJ_VENDOR",
]

VENDOR: str = "spotify"
ODJ_VENDOR: str = "webui"
VENDOR: str = "spotify"


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


@dataclass(frozen=True)
class WriteSummary:
    """What actually happened during a ``--live`` write.

    Frozen so callers can treat the returned summary as an immutable
    record. ``backup_path`` / ``reversal_script_path`` are injected by
    the caller (importer) via keyword args to
    :func:`write_playlist_and_pending` so the summary never carries
    sentinel values.
    """

    playlist_id: str
    vendor_pl_id: str
    snapshot_id: str
    backup_path: Path
    reversal_script_path: Path
    matched_written: int
    pending_written: int
    skipped_existing_snapshot: bool = False
    odj_playlist_id: str | None = None
    odj_created: bool = False
    vendor_ids_set: int = 0
    synthetic_tracks_written: int = 0


@dataclass(frozen=True)
class PlaylistLink:
    vendor_pl_id: str
    spotify_playlist_id: str
    odj_playlist_id: str
    created_at: str
    updated_at: str


def backup_state_db(
    state_db_path: Path | None = None,
    *,
    backup_dir: Path | None = None,
) -> Path:
    """Snapshot the state DB via the SQLite ``.backup`` API."""
    src = Path(state_db_path) if state_db_path else state_paths.STATE_DB
    if not src.exists():
        raise FileNotFoundError(f"state DB not found at {src}")

    dest_dir = (
        Path(backup_dir)
        if backup_dir is not None
        else state_paths.DATA_DIR / "spotify" / "backups"
    )
    dest_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = dest_dir / f"state-{ts}.db"

    with sqlite3.connect(str(src)) as src_conn:
        with sqlite3.connect(str(dest)) as dest_conn:
            src_conn.backup(dest_conn)
    return dest


def emit_reversal_script(
    backup_path: Path,
    state_db_path: Path | None = None,
    *,
    out_dir: Path | None = None,
) -> Path:
    """Write a standalone Python script that restores ``backup_path``.

    The generated script does NOT import from ``apps.spotify`` -- it
    keeps working even if the module layout shifts.
    """
    target = Path(state_db_path) if state_db_path else state_paths.STATE_DB
    out_root = Path(out_dir) if out_dir is not None else backup_path.parent
    out_root.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    script_path = out_root / f"reverse-{ts}.py"

    script = f'''"""Auto-generated reversal script for a Spotify import.

Running this restores the state DB to the pre-import backup. Idempotent,
but running it will destroy any later writes; diff before running.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

BACKUP = Path({str(backup_path)!r})
TARGET = Path({str(target)!r})


def main() -> int:
    if not BACKUP.exists():
        print(f"backup not found: {{BACKUP}}", file=sys.stderr)
        return 1
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(BACKUP, TARGET)
    print(f"restored {{TARGET}} from {{BACKUP}}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''
    # P09-F02: reversal scripts may contain non-ASCII paths; pin UTF-8.
    script_path.write_text(script, encoding="utf-8")
    return script_path


def _state_playlist_id(vendor_pl_id: str) -> str:
    """Deterministic state-layer id for a Spotify playlist."""
    return f"spotify:{vendor_pl_id}"


def synthetic_stable_id(spotify_uri: str) -> str:
    """Stable id for an unmatched Spotify track (no local file yet).

    Deterministic so re-imports reuse the same row and membership instead of
    spawning duplicates. Prefix keeps these easy to grep in the DB.
    """
    digest = hashlib.sha1(spotify_uri.encode("utf-8")).hexdigest()
    return f"spotify-pending:{digest}"


def _spotify_vendor_track_id(src: SpotifyTrack) -> str | None:
    """Canonical vendor id for track_vendor_ids (bare Spotify track id)."""
    if src.spotify_id:
        return src.spotify_id
    uri = src.spotify_uri or ""
    if uri.startswith("spotify:track:"):
        return uri.split(":", 2)[2] or None
    return None


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
    ts = now or datetime.now(timezone.utc).isoformat()
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
    ts = now or datetime.now(timezone.utc).isoformat()
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


def _upsert_synthetic_track(
    conn: sqlite3.Connection,
    src: SpotifyTrack,
    *,
    now: str,
) -> str:
    """Insert/update a placeholder track row for an unmatched Spotify track.

    ``file_path`` is the Spotify URI so ``is_streaming_path`` marks the row
    streaming (not broken-missing) in the browser.
    """
    sid = synthetic_stable_id(src.spotify_uri)
    artists_json = json.dumps(
        list(src.artists), sort_keys=False, separators=(",", ":"), ensure_ascii=False
    )
    tier = "isrc" if src.isrc else "inferred"
    file_path = src.spotify_uri or f"spotify:track:{src.spotify_id or sid}"
    existing = conn.execute(
        "SELECT stable_id FROM tracks WHERE stable_id = ?", (sid,)
    ).fetchone()
    if existing is None:
        conn.execute(
            """
            INSERT INTO tracks
              (stable_id, stable_id_tier, title, artists_json, album, isrc,
               duration_ms, file_path, content_hash, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                sid,
                tier,
                src.title,
                artists_json,
                src.album or None,
                src.isrc,
                src.duration_ms or None,
                file_path,
                now,
                now,
            ),
        )
    else:
        conn.execute(
            """
            UPDATE tracks SET stable_id_tier=?, title=?, artists_json=?,
              album=?, isrc=?, duration_ms=?, file_path=?, updated_at=?
            WHERE stable_id=?
            """,
            (
                tier,
                src.title,
                artists_json,
                src.album or None,
                src.isrc,
                src.duration_ms or None,
                file_path,
                now,
                sid,
            ),
        )
    vendor_id = _spotify_vendor_track_id(src)
    if vendor_id:
        conn.execute(
            "INSERT OR REPLACE INTO track_vendor_ids(stable_id, vendor, vendor_id) "
            "VALUES (?, ?, ?)",
            (sid, VENDOR, vendor_id),
        )
    return sid


def _set_track_vendor_id(
    conn: sqlite3.Connection,
    stable_id: str,
    vendor_id: str,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO track_vendor_ids(stable_id, vendor, vendor_id) "
        "VALUES (?, ?, ?)",
        (stable_id, VENDOR, vendor_id),
    )


_SUGGESTED_SOURCES_TEMPLATE: tuple[tuple[str, str], ...] = (
    ("beatport", "https://www.beatport.com/search?q={q}"),
    ("bandcamp", "https://bandcamp.com/search?q={q}&item_type=t"),
    ("qobuz", "https://www.qobuz.com/us-en/search?q={q}"),
    ("apple_music", "https://music.apple.com/us/search?term={q}"),
    ("discogs", "https://www.discogs.com/search?q={q}&type=release"),
)

SUGGESTED_SOURCE_KEYS: tuple[str, ...] = tuple(
    source_name for source_name, _ in _SUGGESTED_SOURCES_TEMPLATE
)


def _suggested_sources(track: SpotifyTrack) -> dict[str, str]:
    q = quote_plus(f"{track.artists_joined} {track.title}".strip())
    return {name: tmpl.format(q=q) for name, tmpl in _SUGGESTED_SOURCES_TEMPLATE}


def write_playlist_and_pending(
    conn: sqlite3.Connection,
    playlist: SpotifyPlaylist,
    result: MatchResult,
    *,
    backup_path: Path,
    reversal_script_path: Path,
    force: bool = False,
    link_odj: bool = True,
) -> WriteSummary:
    """Insert / refresh playlist + memberships + pending + ODJ twin.

    All writes happen inside one transaction. Caller must have taken
    the backup + written the reversal script BEFORE calling (so a
    mid-commit crash is still recoverable) and must pass those paths
    in as ``backup_path`` / ``reversal_script_path`` so the returned
    :class:`WriteSummary` is self-consistent and free of sentinels.

    When ``link_odj`` is True (default), an unlinked Spotify playlist
    gets a same-name ``webui`` ODJ twin and a ``spotify_playlist_links``
    row; memberships (matched + synthetic pending) are mirrored onto the
    ODJ playlist so the main browser shows green unmatched rows.
    """
    ensure_aux_tables(conn)

    playlist_id = _state_playlist_id(playlist.id)
    matched_rows: list[tuple[str, str, int]] = []
    pending_rows: list[tuple] = []
    odj_playlist_id: str | None = None
    odj_created = False
    vendor_ids_set = 0
    synthetic_tracks_written = 0

    conn.execute("BEGIN IMMEDIATE")
    try:
        # The snapshot short-circuit is an idempotency precondition, so it
        # belongs after the writer lock. Two import processes otherwise can
        # both observe the old snapshot and each rewrite the same playlist.
        prior_snapshot = already_imported_snapshot(conn, playlist.id)
        if prior_snapshot == playlist.snapshot_id and not force:
            existing_link = fetch_playlist_link(conn, playlist.id)
            conn.execute("COMMIT")
            return WriteSummary(
                playlist_id=playlist_id,
                vendor_pl_id=playlist.id,
                snapshot_id=playlist.snapshot_id,
                backup_path=backup_path,
                reversal_script_path=reversal_script_path,
                matched_written=0,
                pending_written=0,
                skipped_existing_snapshot=True,
                odj_playlist_id=(
                    existing_link.odj_playlist_id if existing_link else None
                ),
            )

        now = datetime.now(timezone.utc).isoformat()
        revision = next_playlist_revision(conn, playlist_id, now)
        conn.execute(
            """
            INSERT INTO playlists
              (playlist_id, name, vendor, vendor_pl_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(playlist_id) DO UPDATE SET
              name = excluded.name,
              updated_at = excluded.updated_at
            """,
            (playlist_id, playlist.name, VENDOR, playlist.id, now, revision),
        )

        # Build membership in Spotify order: matched local OR synthetic pending.
        membership: list[tuple[str, str, int]] = []
        for idx, pair in enumerate(result.pairs):
            if pair.status == "matched" and pair.target is not None:
                sid = pair.target.stable_id
                membership.append((playlist_id, sid, idx))
                matched_rows.append((playlist_id, sid, idx))
                vendor_id = _spotify_vendor_track_id(pair.source)
                if vendor_id:
                    _set_track_vendor_id(conn, sid, vendor_id)
                    vendor_ids_set += 1
            else:
                src = pair.source
                sid = _upsert_synthetic_track(conn, src, now=now)
                synthetic_tracks_written += 1
                membership.append((playlist_id, sid, idx))
                pending_rows.append(
                    (
                        playlist_id,
                        idx,
                        src.spotify_uri,
                        src.isrc,
                        src.title,
                        src.artists_joined,
                        src.album,
                        src.duration_ms,
                        json.dumps(_suggested_sources(src), ensure_ascii=False),
                        "pending",
                        now,
                    )
                )

        conn.execute(
            "DELETE FROM playlist_memberships WHERE playlist_id = ?",
            (playlist_id,),
        )
        if membership:
            conn.executemany(
                "INSERT INTO playlist_memberships (playlist_id, stable_id, position)"
                " VALUES (?, ?, ?)",
                membership,
            )

        conn.execute(
            "DELETE FROM pending_tracks WHERE playlist_id = ? AND status = 'pending'",
            (playlist_id,),
        )
        if pending_rows:
            conn.executemany(
                """
                INSERT INTO pending_tracks
                  (playlist_id, position, spotify_uri, isrc, title, artist,
                   album, duration_ms, suggested_sources_json, status, added_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                pending_rows,
            )

        conn.execute(
            """
            INSERT INTO spotify_playlist_meta
              (playlist_id, vendor_pl_id, snapshot_id, track_count,
               matched_count, pending_count, last_import_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(playlist_id) DO UPDATE SET
              snapshot_id = excluded.snapshot_id,
              track_count = excluded.track_count,
              matched_count = excluded.matched_count,
              pending_count = excluded.pending_count,
              last_import_at = excluded.last_import_at
            """,
            (
                playlist_id,
                playlist.id,
                playlist.snapshot_id,
                len(result.pairs),
                len(matched_rows),
                len(pending_rows),
                now,
            ),
        )

        if link_odj:
            link, odj_created = ensure_odj_link(
                conn,
                vendor_pl_id=playlist.id,
                spotify_playlist_id=playlist_id,
                playlist_name=playlist.name,
                now=now,
            )
            odj_playlist_id = link.odj_playlist_id
            # Mirror full membership onto the ODJ twin (matched + pending).
            odj_membership = [
                (odj_playlist_id, sid, pos) for (_pl, sid, pos) in membership
            ]
            conn.execute(
                "DELETE FROM playlist_memberships WHERE playlist_id = ?",
                (odj_playlist_id,),
            )
            if odj_membership:
                conn.executemany(
                    "INSERT INTO playlist_memberships "
                    "(playlist_id, stable_id, position) VALUES (?, ?, ?)",
                    odj_membership,
                )
            odj_revision = next_playlist_revision(conn, odj_playlist_id, now)
            conn.execute(
                "UPDATE playlists SET name = ?, updated_at = ? WHERE playlist_id = ?",
                (playlist.name, odj_revision, odj_playlist_id),
            )

        conn.execute(
            """
            INSERT INTO adapters (adapter_id, last_run_at, last_ok, notes)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(adapter_id) DO UPDATE SET
              last_run_at = excluded.last_run_at,
              last_ok = 1,
              notes = excluded.notes
            """,
            (
                VENDOR,
                now,
                f"imported playlist {playlist.id} snapshot={playlist.snapshot_id}",
            ),
        )

        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return WriteSummary(
        playlist_id=playlist_id,
        vendor_pl_id=playlist.id,
        snapshot_id=playlist.snapshot_id,
        backup_path=backup_path,
        reversal_script_path=reversal_script_path,
        matched_written=len(matched_rows),
        pending_written=len(pending_rows),
        skipped_existing_snapshot=False,
        odj_playlist_id=odj_playlist_id,
        odj_created=odj_created,
        vendor_ids_set=vendor_ids_set,
        synthetic_tracks_written=synthetic_tracks_written,
    )


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
    now = datetime.now(timezone.utc).isoformat()
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


def open_state_rw_with_aux(path: Path | None = None) -> sqlite3.Connection:
    """Open the state DB rw and ensure Phase-9 aux tables exist."""
    conn = state_db.open_rw(path)
    ensure_aux_tables(conn)
    return conn
