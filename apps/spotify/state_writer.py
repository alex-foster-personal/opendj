"""Shared-state writes for Phase 9 Spotify imports.

Phase 5 state layer has ``playlists(vendor, vendor_pl_id, ...)``
already. Phase 9 adds two additive tables (created IF NOT EXISTS at
first call) for concepts Phase 5 doesn't own:

* ``spotify_playlist_meta`` -- per-playlist snapshot_id for idempotent
  re-imports.
* ``pending_tracks`` -- one row per unmatched Spotify track, with
  suggested purchase sources (acquisition queue).

Every live write:
    1. backup the state DB via the SQLite ``.backup`` API.
    2. write a self-contained reversal script next to the backup.
    3. run the inserts in one transaction; rollback on any exception.
    4. upsert the adapter registry row.

Dry-run mode never reaches ``write_playlist_and_pending``; the
importer emits artifacts only. See CONTEXT D6.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import quote_plus

from apps.shared.state import db as state_db
from apps.shared.state import paths as state_paths
from apps.shared.state import sync_stamp
from apps.shared.state.writer import next_playlist_revision

from .client import SpotifyPlaylist, SpotifyTrack
from .matcher_adapter import MatchResult

__all__ = [
    "AUX_MIGRATIONS",
    "WriteSummary",
    "PendingRow",
    "ensure_aux_tables",
    "backup_state_db",
    "emit_reversal_script",
    "write_playlist_and_pending",
    "already_imported_snapshot",
    "fetch_pending_tracks",
    "mark_pending_abandoned",
    "open_state_rw_with_aux",
    "SUGGESTED_SOURCE_KEYS",
    "VENDOR",
]


VENDOR: str = "spotify"

# The two synced tables this importer writes. Named so the row write and its
# ``local_changelog`` entry can never disagree about which table changed.
PLAYLISTS_TABLE: str = "playlists"
MEMBERSHIPS_TABLE: str = "playlist_memberships"


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

AUX_MIGRATIONS: tuple[str, ...] = (
    _AUX_DDL_PLAYLIST_META,
    _AUX_DDL_PENDING,
    _AUX_IDX_PENDING_PLAYLIST,
    _AUX_IDX_PENDING_ISRC,
    _AUX_IDX_PENDING_STATUS,
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
) -> WriteSummary:
    """Insert / refresh playlist + memberships + pending rows.

    All writes happen inside one transaction. Caller must have taken
    the backup + written the reversal script BEFORE calling (so a
    mid-commit crash is still recoverable) and must pass those paths
    in as ``backup_path`` / ``reversal_script_path`` so the returned
    :class:`WriteSummary` is self-consistent and free of sentinels.
    """
    ensure_aux_tables(conn)

    playlist_id = _state_playlist_id(playlist.id)

    conn.execute("BEGIN IMMEDIATE")
    try:
        # The snapshot short-circuit is an idempotency precondition, so it
        # belongs after the writer lock. Two import processes otherwise can
        # both observe the old snapshot and each rewrite the same playlist.
        prior_snapshot = already_imported_snapshot(conn, playlist.id)
        if prior_snapshot == playlist.snapshot_id and not force:
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
            )

        # Round 2 finding N1c: this importer stamped neither
        # origin_device_id nor local_changelog, so an imported playlist was
        # never offered to the hub and every later sync failed its digest
        # compare on ['playlist_memberships', 'playlists'] permanently. The
        # empty tiebreak component was round 1 finding 2's second half, still
        # live on this path. Both tables now go through the shared chokepoint.
        machine_id = sync_stamp.ensure_local_machine(conn)
        now = sync_stamp.canonical_now()
        revision = next_playlist_revision(conn, playlist_id, now)
        playlist_stamp = sync_stamp.stamp_and_log(
            conn, PLAYLISTS_TABLE, (playlist_id,), machine_id, now=revision,
        )
        conn.execute(
            """
            INSERT INTO playlists
              (playlist_id, name, vendor, vendor_pl_id, created_at,
               updated_at, origin_device_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(playlist_id) DO UPDATE SET
              name = excluded.name,
              updated_at = excluded.updated_at,
              origin_device_id = excluded.origin_device_id,
              deleted_at = NULL
            """,
            (
                playlist_id, playlist.name, VENDOR, playlist.id, now,
                playlist_stamp.updated_at, playlist_stamp.origin_device_id,
            ),
        )
        # deleted_at = NULL on conflict (ADR 08 point 5): playlist_id is
        # deterministic (f"spotify:{vendor_pl_id}"), so a re-import can land
        # on a row this machine soft-deleted locally. Without clearing the
        # tombstone here the playlist would re-import silently invisible
        # forever -- see apps.shared.state.writer.StateWriter.insert_playlist
        # for the same reactivation.
        #
        # The membership replace below is the established whole-playlist
        # pattern (matches StateWriter.set_playlist_memberships and
        # apps.sync_hub.engine._replace_members): every position is
        # overwritten on every import, so there is no independent "this one
        # membership row was removed" event to tombstone -- the fresh INSERT
        # below is what makes the current state correct either way. The
        # playlists stamp above is what carries the whole bundle to the hub
        # (ADR 04 c5); the per-row stamps are what let the push fence see
        # that this playlist changed at all.
        conn.execute(
            "DELETE FROM playlist_memberships WHERE playlist_id = ?",
            (playlist_id,),
        )
        matched_rows = [
            (playlist_id, pair.target.stable_id, idx)
            for idx, pair in enumerate(result.pairs)
            if pair.status == "matched" and pair.target is not None
        ]
        for member in matched_rows:
            member_stamp = sync_stamp.stamp_and_log(
                conn,
                MEMBERSHIPS_TABLE,
                (playlist_id, member[2]),
                machine_id,
                now=playlist_stamp.updated_at,
            )
            conn.execute(
                "INSERT INTO playlist_memberships (playlist_id, stable_id, "
                "position, updated_at, origin_device_id) VALUES (?, ?, ?, ?, ?)",
                (
                    *member, member_stamp.updated_at,
                    member_stamp.origin_device_id,
                ),
            )

        conn.execute(
            "DELETE FROM pending_tracks WHERE playlist_id = ? AND status = 'pending'",
            (playlist_id,),
        )
        pending_rows = []
        for idx, pair in enumerate(result.pairs):
            if pair.status == "matched":
                continue
            src = pair.source
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
