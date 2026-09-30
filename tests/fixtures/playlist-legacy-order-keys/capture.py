"""Capture legacy playlist order_key shapes written by the pre-LIBM-132 add path.

Run ONLY from a checkout of the pinned commit named in manifest.json, with that
checkout's own interpreter and package on the path, e.g.:

    cd <checkout at the pinned sha>
    PYTHONPATH=$PWD .venv/bin/python <this file> <out.sql>

Every row is written by that commit's production code: the Spotify importer's
live write (``match_spotify_tracks`` + ``write_playlist_and_pending``, the same
sequence ``apps.spotify.importer.run_import`` runs) and the webui
``PlaylistStore`` that ``routes/playlist_write.py`` builds. Nothing here writes
an order_key. After capture, identity-bearing columns are replaced by
placeholders (see SANITIZE) and the database is dumped with ``iterdump``.

The script refuses to run on any tree whose ``order_key.between`` no longer
returns the legacy empty key, so it cannot silently capture today's shapes.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

import apps
from apps.shared.state import db as state_db
from apps.shared.state.order_key import between
from apps.shared.state.schema import SCHEMA_VERSION
from apps.spotify.client import SpotifyPlaylist, SpotifyTrack
from apps.spotify.matcher_adapter import load_local_tracks, match_spotify_tracks
from apps.spotify.state_writer import (
    backup_state_db,
    emit_reversal_script,
    open_state_rw_with_aux,
    write_playlist_and_pending,
)
from apps.webui.server.playlist_store import PlaylistStore

LEGACY_SCHEMA_VERSION = 20
SPOTIFY_TRACKS = 12
BATCH_APPEND = 60  # "00000004" + 60 * "V" is 68 chars, past MAX_ORDER_KEY_LEN (64)

# column -> placeholder; every other column holds generated ids, timestamps or
# the synthetic fixture strings below.
SANITIZE = {
    ("machines", "name"): "fixture-host",
    ("machines", "data_root"): "/Users/dev/opendj-data",
    ("machines", "platform"): "macos",
}


def _require_legacy_tree() -> None:
    checkout = Path(apps.__file__).resolve().parents[1]
    print(f"[OK] capturing with apps from {checkout}")
    if SCHEMA_VERSION != LEGACY_SCHEMA_VERSION:
        raise SystemExit(f"[ERROR] schema v{SCHEMA_VERSION}, expected v{LEGACY_SCHEMA_VERSION}")
    if between(None, "00000000") != "":
        raise SystemExit("[ERROR] order_key.between is not the legacy version; wrong checkout")


def _spotify_playlist() -> SpotifyPlaylist:
    tracks = tuple(
        SpotifyTrack(
            spotify_id=f"FixtureTrack{i:010d}",
            spotify_uri=f"spotify:track:FixtureTrack{i:010d}",
            isrc=None,
            title=f"Fixture Track {i:02d}",
            artists=("Fixture Artist",),
            album="Fixture Album",
            duration_ms=180_000 + i,
            is_local=False,
        )
        for i in range(SPOTIFY_TRACKS)
    )
    return SpotifyPlaylist(
        id="FixturePlaylist0000001",
        name="Imported from Spotify",
        snapshot_id="fixture-snapshot-1",
        owner="fixture-owner",
        description="",
        tracks=tracks,
    )


def _import_from_spotify(db_path: Path, work: Path) -> None:
    """The live half of run_import, with the fetch replaced by its parsed result."""
    playlist = _spotify_playlist()
    ro = state_db.open_ro(db_path)
    try:
        targets = load_local_tracks(ro)
    finally:
        ro.close()
    result = match_spotify_tracks(playlist.tracks, targets)
    assert all(p.status == "unmatched" for p in result.pairs), "an empty library matched"
    backup = backup_state_db(db_path, backup_dir=work / "backups")
    reversal = emit_reversal_script(backup, db_path, out_dir=work / "backups")
    conn = open_state_rw_with_aux(db_path)
    try:
        summary = write_playlist_and_pending(
            conn, playlist, result, backup_path=backup, reversal_script_path=reversal,
        )
    finally:
        conn.close()
    assert summary.odj_playlist_id, "the Spotify import made no ODJ twin"


def _webui_edits(db_path: Path) -> None:
    store = PlaylistStore(db_path)
    try:
        twin_id = store._conn.execute(
            "SELECT playlist_id FROM playlists WHERE vendor = 'webui' AND name = ?",
            ("Imported from Spotify",),
        ).fetchone()[0]
        sids = [
            row[0]
            for row in store._conn.execute(
                "SELECT stable_id FROM playlist_memberships WHERE playlist_id = ? "
                "ORDER BY position",
                (twin_id,),
            )
        ]
        assert len(sids) == SPOTIFY_TRACKS, sids

        # 1. The Spotify twin edited in the webui: keyed rows between NULL-key
        #    rows, and one of them removed again.
        store.add_memberships(twin_id, [sids[0]], position=3)
        store.add_memberships(twin_id, [sids[1]], position=8)
        store.add_memberships(twin_id, [sids[2]], position=10)
        added = [
            r[0] for r in store._conn.execute(
                "SELECT item_id FROM playlist_memberships WHERE playlist_id = ? "
                "AND stable_id = ? AND item_id IS NOT NULL", (twin_id, sids[2]),
            )
        ]
        assert len(added) == 1, added
        store.remove_memberships(twin_id, added)

        # 2. Two adds at the head of a fresh list: the first gets the empty
        #    key, the second shares the last member's key and lands last.
        head = store.create_playlist("Head inserts")
        head = store.replace_memberships(head.playlist_id, sids[:5], expected_etag=head.etag)
        store.add_memberships(head.playlist_id, [sids[5]], position=0)
        store.add_memberships(head.playlist_id, [sids[6]], position=0)

        # 3. One batch append: each key extends the previous by "V".
        tail = store.create_playlist("Batch append")
        tail = store.replace_memberships(tail.playlist_id, sids[:5], expected_etag=tail.etag)
        batch = [sids[i % SPOTIFY_TRACKS] for i in range(BATCH_APPEND)]
        store.add_memberships(tail.playlist_id, batch)
    finally:
        store.close()


def _sanitize(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        for (table, column), value in SANITIZE.items():
            conn.execute(f"UPDATE {table} SET {column} = ?", (value,))
        conn.commit()
    finally:
        conn.close()


def main(out: Path) -> int:
    _require_legacy_tree()
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        db_path = work / "data" / "state" / "state.db"
        db_path.parent.mkdir(parents=True)
        state_db.open_rw(db_path).close()
        _import_from_spotify(db_path, work)
        _webui_edits(db_path)
        _sanitize(db_path)
        conn = sqlite3.connect(str(db_path))
        try:
            out.write_text("\n".join(conn.iterdump()) + "\n", encoding="utf-8")
        finally:
            conn.close()
    print(f"[OK] wrote {out} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1])))
