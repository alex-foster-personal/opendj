"""Tests for apps.spotify.rematch."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from apps.shared.state import schema as state_schema
from apps.spotify.client import SpotifyPlaylist, SpotifyTrack
from apps.spotify.matcher_adapter import MatchedPair, MatchResult
from apps.spotify.rematch import rematch_playlist
from apps.spotify.state_writer import (
    _state_playlist_id,
    ensure_aux_tables,
    fetch_pending_tracks,
    write_playlist_and_pending,
)
from apps.webui.server.backend import ConflictError
from apps.webui.server.playlist_store import PlaylistStore


@pytest.fixture
def state_conn(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "state.db", isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    state_schema.apply_migrations(conn)
    ensure_aux_tables(conn)
    return conn


def _insert_track(conn: sqlite3.Connection, sid: str, isrc: str | None,
                  title="Song", artists=("Artist",), duration_ms=200000) -> None:
    conn.execute(
        """INSERT INTO tracks
           (stable_id, stable_id_tier, title, artists_json, album, isrc,
            duration_ms, file_path, content_hash, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'alb', ?, ?, NULL, NULL, '2026', '2026')""",
        (sid, "isrc" if isrc else "inferred", title, json.dumps(list(artists)),
         isrc, duration_ms),
    )


def _seed_unmatched_playlist(conn: sqlite3.Connection, pid="pl123") -> str:
    src = SpotifyTrack(
        spotify_id="t1", spotify_uri="spotify:track:t1",
        isrc="USABC2500001", title="Hello", artists=("Artist",),
        album="A", duration_ms=200000, is_local=False,
    )
    result = MatchResult(pairs=[MatchedPair(src, None, 0.0, (), "unmatched")])
    playlist = SpotifyPlaylist(
        id=pid, name="P", snapshot_id="s1", owner="o", description="",
        tracks=(src,),
    )
    write_playlist_and_pending(
        conn,
        playlist,
        result,
        backup_path=Path("/tmp/test-backup.db"),
        reversal_script_path=Path("/tmp/test-reverse.py"),
    )
    return _state_playlist_id(pid)


@pytest.mark.requirement("CAT-01")
def test_rematch_dry_run(state_conn: sqlite3.Connection) -> None:
    playlist_id = _seed_unmatched_playlist(state_conn)
    _insert_track(state_conn, "s1", "USABC2500001", "Hello")

    outcome = rematch_playlist(state_conn, playlist_id, live=False)
    assert outcome.resolved_count == 1
    assert outcome.still_pending_count == 0
    assert len(fetch_pending_tracks(state_conn, playlist_id, status="pending")) == 1


@pytest.mark.requirement("CAT-01")
def test_rematch_live_promotes(state_conn: sqlite3.Connection) -> None:
    playlist_id = _seed_unmatched_playlist(state_conn)
    _insert_track(state_conn, "s1", "USABC2500001", "Hello")

    outcome = rematch_playlist(state_conn, playlist_id, live=True)
    assert outcome.resolved_count == 1

    memberships = state_conn.execute(
        "SELECT stable_id FROM playlist_memberships WHERE playlist_id = ?",
        (playlist_id,),
    ).fetchall()
    assert memberships == [("s1",)]

    assert fetch_pending_tracks(state_conn, playlist_id, status="pending") == []
    resolved = fetch_pending_tracks(state_conn, playlist_id, status="resolved")
    assert len(resolved) == 1
    assert resolved[0].resolved_stable_id == "s1"


@pytest.mark.requirement("CAT-01")
def test_rematch_rotates_playlist_revision_and_rejects_stale_replace(
    state_conn: sqlite3.Connection,
) -> None:
    playlist_id = _seed_unmatched_playlist(state_conn)
    _insert_track(state_conn, "s1", "USABC2500001", "Hello")
    db_path = Path(state_conn.execute("PRAGMA database_list").fetchone()[2])
    store = PlaylistStore(db_path)
    try:
        stale_etag = store.get_playlist_row(playlist_id).etag
        rematch_playlist(state_conn, playlist_id, live=True)

        with pytest.raises(ConflictError) as raised:
            store.replace_memberships(
                playlist_id, ["s1", "s1"], expected_etag=stale_etag,
            )

        assert raised.value.current["items"] == ["s1"]
        assert raised.value.etag != stale_etag
    finally:
        store.close()


@pytest.mark.requirement("CAT-01")
def test_rematch_no_pending_is_noop(state_conn: sqlite3.Connection) -> None:
    outcome = rematch_playlist(state_conn, "spotify:doesnotexist", live=True)
    assert outcome.resolved_count == 0
    assert outcome.still_pending_count == 0


@pytest.mark.requirement("CAT-01")
def test_rematch_idempotent(state_conn: sqlite3.Connection) -> None:
    playlist_id = _seed_unmatched_playlist(state_conn)
    _insert_track(state_conn, "s1", "USABC2500001", "Hello")

    rematch_playlist(state_conn, playlist_id, live=True)
    outcome2 = rematch_playlist(state_conn, playlist_id, live=True)
    assert outcome2.resolved_count == 0


@pytest.mark.requirement("CAT-01")
def test_rematch_still_pending_without_local_match(
    state_conn: sqlite3.Connection,
) -> None:
    playlist_id = _seed_unmatched_playlist(state_conn)
    outcome = rematch_playlist(state_conn, playlist_id, live=True)
    assert outcome.resolved_count == 0
    assert outcome.still_pending_count == 1
