"""Shared-state writes for Phase 9 Spotify imports.

Phase 5 state layer has ``playlists(vendor, vendor_pl_id, ...)``
already. Phase 9 adds additive tables for concepts Phase 5 doesn't
own; their DDL, row types, and queries live in :mod:`state_aux` and
are re-exported here so importers keep a single entry point.

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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote_plus

from apps.shared.state import paths as state_paths
from apps.shared.state.writer import next_playlist_revision

from .client import SpotifyPlaylist, SpotifyTrack
from .matcher_adapter import MatchResult
from .state_aux import (
    AUX_MIGRATIONS,
    ODJ_VENDOR,
    VENDOR,
    PendingRow,
    PlaylistLink,
    already_imported_snapshot,
    ensure_aux_tables,
    ensure_odj_link,
    fetch_pending_tracks,
    fetch_playlist_link,
    link_odj_playlist,
    mark_pending_abandoned,
    open_state_rw_with_aux,
)

__all__ = [
    "AUX_MIGRATIONS",
    "ODJ_VENDOR",
    "SUGGESTED_SOURCE_KEYS",
    "VENDOR",
    "PendingRow",
    "PlaylistLink",
    "WriteSummary",
    "already_imported_snapshot",
    "backup_state_db",
    "emit_reversal_script",
    "ensure_aux_tables",
    "ensure_odj_link",
    "fetch_pending_tracks",
    "fetch_playlist_link",
    "link_odj_playlist",
    "mark_pending_abandoned",
    "open_state_rw_with_aux",
    "synthetic_stable_id",
    "write_playlist_and_pending",
]


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
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
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
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
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


@dataclass
class _MembershipBuild:
    """Working set produced while walking match pairs in Spotify order."""

    membership: list[tuple[str, str, int]]
    matched_rows: list[tuple[str, str, int]]
    pending_rows: list[tuple]
    vendor_ids_set: int
    synthetic_tracks_written: int


def _build_membership(
    conn: sqlite3.Connection,
    playlist_id: str,
    result: MatchResult,
    *,
    now: str,
) -> _MembershipBuild:
    """Walk match pairs: matched local rows OR synthetic pending rows."""
    build = _MembershipBuild([], [], [], 0, 0)
    for idx, pair in enumerate(result.pairs):
        if pair.status == "matched" and pair.target is not None:
            sid = pair.target.stable_id
            build.membership.append((playlist_id, sid, idx))
            build.matched_rows.append((playlist_id, sid, idx))
            vendor_id = _spotify_vendor_track_id(pair.source)
            if vendor_id:
                _set_track_vendor_id(conn, sid, vendor_id)
                build.vendor_ids_set += 1
        else:
            src = pair.source
            sid = _upsert_synthetic_track(conn, src, now=now)
            build.synthetic_tracks_written += 1
            build.membership.append((playlist_id, sid, idx))
            build.pending_rows.append(
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
    return build


def _mirror_membership_to_odj(
    conn: sqlite3.Connection,
    playlist: SpotifyPlaylist,
    playlist_id: str,
    membership: list[tuple[str, str, int]],
    *,
    now: str,
) -> tuple[str, bool]:
    """Ensure the ODJ twin exists and mirror full membership onto it."""
    link, odj_created = ensure_odj_link(
        conn,
        vendor_pl_id=playlist.id,
        spotify_playlist_id=playlist_id,
        playlist_name=playlist.name,
        now=now,
    )
    odj_playlist_id = link.odj_playlist_id
    odj_membership = [(odj_playlist_id, sid, pos) for (_pl, sid, pos) in membership]
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
    return odj_playlist_id, odj_created


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
    odj_playlist_id: str | None = None
    odj_created = False

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

        now = datetime.now(UTC).isoformat()
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

        build = _build_membership(conn, playlist_id, result, now=now)

        conn.execute(
            "DELETE FROM playlist_memberships WHERE playlist_id = ?",
            (playlist_id,),
        )
        if build.membership:
            conn.executemany(
                "INSERT INTO playlist_memberships (playlist_id, stable_id, position)"
                " VALUES (?, ?, ?)",
                build.membership,
            )

        conn.execute(
            "DELETE FROM pending_tracks WHERE playlist_id = ? AND status = 'pending'",
            (playlist_id,),
        )
        if build.pending_rows:
            conn.executemany(
                """
                INSERT INTO pending_tracks
                  (playlist_id, position, spotify_uri, isrc, title, artist,
                   album, duration_ms, suggested_sources_json, status, added_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                build.pending_rows,
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
                len(build.matched_rows),
                len(build.pending_rows),
                now,
            ),
        )

        if link_odj:
            odj_playlist_id, odj_created = _mirror_membership_to_odj(
                conn, playlist, playlist_id, build.membership, now=now
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
        matched_written=len(build.matched_rows),
        pending_written=len(build.pending_rows),
        skipped_existing_snapshot=False,
        odj_playlist_id=odj_playlist_id,
        odj_created=odj_created,
        vendor_ids_set=build.vendor_ids_set,
        synthetic_tracks_written=build.synthetic_tracks_written,
    )
