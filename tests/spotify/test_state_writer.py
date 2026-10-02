"""Tests for apps.spotify.state_writer."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from apps.shared.state import schema as state_schema
from apps.shared.state import sync_stamp
from apps.shared.state.writer import StateWriter
from apps.spotify.client import SpotifyPlaylist, SpotifyTrack
from apps.spotify.matcher_adapter import LocalTrack, MatchedPair, MatchResult
from apps.spotify.state_writer import (
    SUGGESTED_SOURCE_KEYS,
    WriteSummary,
    _state_playlist_id,
    already_imported_snapshot,
    backup_state_db,
    emit_reversal_script,
    ensure_aux_tables,
    fetch_pending_tracks,
    fetch_playlist_link,
    mark_pending_abandoned,
    synthetic_stable_id,
    write_playlist_and_pending,
)


@pytest.fixture
def state_conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    state_schema.apply_migrations(conn)
    ensure_aux_tables(conn)
    conn.execute(
        """INSERT INTO tracks
           (stable_id, stable_id_tier, title, artists_json, album, isrc,
            duration_ms, file_path, content_hash, created_at, updated_at)
           VALUES ('s1', 'isrc', 'Hello', ?, 'A', 'USABC2500001', 200000,
                   '/x.mp3', NULL, '2026-01-01', '2026-01-01')""",
        (json.dumps(["Artist A"]),),
    )
    return conn


def _mk_playlist(*, pid="pl123", snapshot="snap-1", tracks=()) -> SpotifyPlaylist:
    return SpotifyPlaylist(
        id=pid, name="Test Playlist", snapshot_id=snapshot,
        owner="dev3", description="", tracks=tracks,
    )


def _mk_src(sid="t1", isrc=None, title="T", **kw) -> SpotifyTrack:
    defaults = dict(
        spotify_id=sid, spotify_uri=f"spotify:track:{sid}",
        isrc=isrc, title=title, artists=("Who",),
        album="Alb", duration_ms=100000, is_local=False,
    )
    defaults.update(kw)
    return SpotifyTrack(**defaults)  # type: ignore[arg-type]


def _mk_local(sid="s1", isrc=None) -> LocalTrack:
    return LocalTrack(stable_id=sid, isrc=isrc, title="x", artists=(), duration_ms=0)


# Sentinel paths for tests that don't care about real backup/reversal files.
# These are required kwargs to write_playlist_and_pending; the function just
# stores them on the returned WriteSummary verbatim.
_TEST_BACKUP = Path("/tmp/test-backup.db")
_TEST_REVERSAL = Path("/tmp/test-reverse.py")


def _write(state_conn, playlist, result, *, force: bool = False):
    """Thin wrapper that supplies the required backup/reversal kwargs."""
    return write_playlist_and_pending(
        state_conn,
        playlist,
        result,
        backup_path=_TEST_BACKUP,
        reversal_script_path=_TEST_REVERSAL,
        force=force,
    )


@pytest.mark.requirement("CAT-01")
def test_ensure_aux_tables_idempotent(state_conn: sqlite3.Connection) -> None:
    ensure_aux_tables(state_conn)
    ensure_aux_tables(state_conn)
    names = {r[0] for r in state_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert "pending_tracks" in names
    assert "spotify_playlist_meta" in names
    assert "spotify_playlist_links" in names


@pytest.mark.requirement("CAT-01")
def test_write_playlist_matched_only(state_conn: sqlite3.Connection) -> None:
    src = _mk_src(sid="t1", isrc="USABC2500001", title="Hello")
    tgt = _mk_local("s1", "USABC2500001")
    result = MatchResult(pairs=[
        MatchedPair(src, tgt, 0.35, ("isrc",), "matched"),
    ])
    playlist = _mk_playlist(pid="pl123", tracks=(src,))

    summary = _write(state_conn, playlist, result)
    assert summary.matched_written == 1
    assert summary.pending_written == 0

    row = state_conn.execute(
        "SELECT name, vendor, vendor_pl_id FROM playlists WHERE playlist_id = ?",
        (_state_playlist_id("pl123"),),
    ).fetchone()
    assert row == ("Test Playlist", "spotify", "pl123")

    rows = state_conn.execute(
        "SELECT stable_id, position FROM playlist_memberships "
        "WHERE playlist_id = ? ORDER BY position",
        (_state_playlist_id("pl123"),),
    ).fetchall()
    assert rows == [("s1", 0)]

    assert already_imported_snapshot(state_conn, "pl123") == "snap-1"
    assert summary.odj_playlist_id is not None
    assert summary.odj_created is True
    link = fetch_playlist_link(state_conn, "pl123")
    assert link is not None
    assert link.odj_playlist_id == summary.odj_playlist_id
    odj_rows = state_conn.execute(
        "SELECT stable_id, position FROM playlist_memberships "
        "WHERE playlist_id = ? ORDER BY position",
        (summary.odj_playlist_id,),
    ).fetchall()
    assert odj_rows == [("s1", 0)]
    vendor = state_conn.execute(
        "SELECT vendor_id FROM track_vendor_ids WHERE stable_id = ? AND vendor = 'spotify'",
        ("s1",),
    ).fetchone()
    assert vendor == ("t1",)


@pytest.mark.requirement("CAT-01")
def test_write_playlist_with_pending(state_conn: sqlite3.Connection) -> None:
    src_matched = _mk_src(sid="t1", isrc="USABC2500001", title="Hello")
    src_unmatched = _mk_src(sid="t2", isrc="ZZZZZ", title="Mystery")
    tgt = _mk_local("s1", "USABC2500001")

    result = MatchResult(pairs=[
        MatchedPair(src_matched, tgt, 0.35, ("isrc",), "matched"),
        MatchedPair(src_unmatched, None, 0.0, (), "unmatched"),
    ])
    playlist = _mk_playlist(pid="pl123", tracks=(src_matched, src_unmatched))

    summary = _write(state_conn, playlist, result)
    assert summary.matched_written == 1
    assert summary.pending_written == 1

    pendings = fetch_pending_tracks(state_conn, _state_playlist_id("pl123"))
    assert len(pendings) == 1
    assert pendings[0].title == "Mystery"
    suggested = json.loads(pendings[0].suggested_sources_json)
    assert set(suggested.keys()) == {
        "beatport", "bandcamp", "qobuz", "apple_music", "discogs",
    }
    assert tuple(suggested) == SUGGESTED_SOURCE_KEYS

    synth = synthetic_stable_id("spotify:track:t2")
    assert summary.synthetic_tracks_written == 1
    members = state_conn.execute(
        "SELECT stable_id, position FROM playlist_memberships "
        "WHERE playlist_id = ? ORDER BY position",
        (_state_playlist_id("pl123"),),
    ).fetchall()
    assert members == [("s1", 0), (synth, 1)]
    track_row = state_conn.execute(
        "SELECT file_path, title FROM tracks WHERE stable_id = ?",
        (synth,),
    ).fetchone()
    assert track_row == ("spotify:track:t2", "Mystery")
    assert summary.odj_playlist_id is not None
    odj_members = state_conn.execute(
        "SELECT stable_id, position FROM playlist_memberships "
        "WHERE playlist_id = ? ORDER BY position",
        (summary.odj_playlist_id,),
    ).fetchall()
    assert odj_members == members


@pytest.mark.requirement("CAT-01")
def test_snapshot_short_circuit(state_conn: sqlite3.Connection) -> None:
    src = _mk_src(sid="t1", isrc="USABC2500001", title="Hello")
    tgt = _mk_local("s1", "USABC2500001")
    result = MatchResult(pairs=[MatchedPair(src, tgt, 0.35, ("isrc",), "matched")])
    playlist = _mk_playlist(pid="pl123", snapshot="snap-1", tracks=(src,))

    _write(state_conn, playlist, result)
    summary2 = _write(state_conn, playlist, result)
    assert summary2.skipped_existing_snapshot is True
    assert summary2.matched_written == 0
    # [I2] regression: the short-circuit summary must carry the real
    # backup/reversal paths passed in, not sentinel /dev/null values.
    assert summary2.backup_path == _TEST_BACKUP
    assert summary2.reversal_script_path == _TEST_REVERSAL


def test_concurrent_same_snapshot_has_one_writer_and_one_short_circuit(
    state_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] two imports share a snapshot [then] only one rewrites state."""
    from apps.spotify import state_writer

    src = _mk_src(sid="t1", isrc="USABC2500001", title="Hello")
    tgt = _mk_local("s1", "USABC2500001")
    result = MatchResult(pairs=[MatchedPair(src, tgt, 0.35, ("isrc",), "matched")])
    playlist = _mk_playlist(pid="race", snapshot="snap-race", tracks=(src,))
    db_path = Path(state_conn.execute("PRAGMA database_list").fetchone()[2])
    first_preflight = threading.Event()
    second_preflight = threading.Event()
    release_first = threading.Event()
    call_count = 0
    call_lock = threading.Lock()
    original_snapshot = state_writer.already_imported_snapshot

    def pause_first_preflight(conn: sqlite3.Connection, vendor_pl_id: str) -> str | None:
        nonlocal call_count
        with call_lock:
            call_count += 1
            is_first = call_count == 1
        if is_first:
            first_preflight.set()
            assert release_first.wait(timeout=5), "test did not release first import"
        else:
            second_preflight.set()
        return original_snapshot(conn, vendor_pl_id)

    monkeypatch.setattr(state_writer, "already_imported_snapshot", pause_first_preflight)
    summaries: list[WriteSummary] = []
    errors: list[BaseException] = []

    def write_once() -> None:
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            summaries.append(_write(conn, playlist, result))
        except BaseException as exc:
            errors.append(exc)
        finally:
            conn.close()

    first = threading.Thread(target=write_once)
    second = threading.Thread(target=write_once)
    first.start()
    assert first_preflight.wait(timeout=5), "first import did not preflight"
    second.start()
    assert not second_preflight.wait(timeout=0.5), (
        "second import bypassed the SQLite writer lock"
    )
    release_first.set()
    first.join(timeout=5)
    second.join(timeout=5)
    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert sorted(s.skipped_existing_snapshot for s in summaries) == [False, True]


@pytest.mark.requirement("CAT-01")
def test_force_overrides_snapshot_short_circuit(state_conn: sqlite3.Connection) -> None:
    src = _mk_src(sid="t1", isrc="USABC2500001", title="Hello")
    tgt = _mk_local("s1", "USABC2500001")
    result = MatchResult(pairs=[MatchedPair(src, tgt, 0.35, ("isrc",), "matched")])
    playlist = _mk_playlist(pid="pl123", snapshot="snap-1", tracks=(src,))

    _write(state_conn, playlist, result)
    summary = _write(state_conn, playlist, result, force=True)
    assert summary.skipped_existing_snapshot is False
    # [I2] regression: full-write path also carries real paths.
    assert summary.backup_path == _TEST_BACKUP
    assert summary.reversal_script_path == _TEST_REVERSAL


@pytest.mark.requirement("CAT-01")
def test_backup_state_db_roundtrip(state_conn: sqlite3.Connection, tmp_path: Path) -> None:
    path = Path(state_conn.execute("PRAGMA database_list").fetchall()[0][2])
    state_conn.close()
    backup = backup_state_db(path, backup_dir=tmp_path / "backups")
    assert backup.exists()
    b = sqlite3.connect(backup)
    try:
        rows = b.execute("SELECT stable_id FROM tracks").fetchall()
        assert rows == [("s1",)]
    finally:
        b.close()


@pytest.mark.requirement("CAT-01")
def test_emit_reversal_script_is_self_contained(tmp_path: Path) -> None:
    backup = tmp_path / "bak.db"
    backup.write_bytes(b"fake")
    target = tmp_path / "state.db"
    script = emit_reversal_script(backup, target, out_dir=tmp_path)
    text = script.read_text()
    assert "apps.spotify" not in text
    # The generator embeds paths via {str(path)!r}; on Windows the repr
    # doubles backslashes, so match the repr form, not the bare str.
    assert repr(str(backup)) in text
    assert repr(str(target)) in text


@pytest.mark.requirement("CAT-01")
def test_mark_pending_abandoned(state_conn: sqlite3.Connection) -> None:
    src = _mk_src(sid="t1", isrc=None, title="Mystery")
    result = MatchResult(pairs=[MatchedPair(src, None, 0.0, (), "unmatched")])
    playlist = _mk_playlist(pid="pl123", tracks=(src,))
    _write(state_conn, playlist, result)

    pendings = fetch_pending_tracks(state_conn, _state_playlist_id("pl123"))
    ids = [p.pending_id for p in pendings]
    rows = mark_pending_abandoned(state_conn, ids)
    assert rows == 1
    assert fetch_pending_tracks(
        state_conn, _state_playlist_id("pl123"), status="pending"
    ) == []
    abandoned = fetch_pending_tracks(
        state_conn, _state_playlist_id("pl123"), status="abandoned"
    )
    assert len(abandoned) == 1


# --- round 2 finding N1c: the importer must reach the push fence ----------


def _changelog(conn: sqlite3.Connection) -> dict[str, int]:
    return dict(
        conn.execute(
            "SELECT table_name, COUNT(*) FROM local_changelog GROUP BY table_name"
        ).fetchall()
    )


def test_an_imported_playlist_is_stamped_and_logged(state_conn: sqlite3.Connection):
    """Round 2 finding N1c, reproduced as a permanent regression.

      [observed] b3 spotify playlist row: ('2026-08-31T10:41:47...', None)
      [observed] b3 local_changelog playlists entries: 0
      [observed] b3 lww_key -> ('2026-08-31T10:41:47.349967+00:00', '')
      [observed] b3 sync 0/1: ... tables ['playlist_memberships',
                                          'playlists'] differ

    Importing a Spotify playlist stopped that machine syncing anything, and
    the empty tiebreak component is round 1 finding 2's second half still
    live on this path.
    """
    result = MatchResult(pairs=[
        MatchedPair(_mk_src("t1"), _mk_local("s1"), 1.0, (), "matched"),
        MatchedPair(_mk_src("t2"), None, 0.0, (), "unmatched"),
    ])
    write_playlist_and_pending(
        state_conn, _mk_playlist(tracks=()), result,
        backup_path=_TEST_BACKUP, reversal_script_path=_TEST_REVERSAL,
    )

    playlist = state_conn.execute(
        "SELECT updated_at, origin_device_id FROM playlists"
    ).fetchone()
    assert playlist[0] and playlist[1], (
        "an unstamped playlist row presents an empty LWW tiebreak and loses "
        "every conflict"
    )
    assert sync_stamp.to_canonical(playlist[0]) == playlist[0]

    members = state_conn.execute(
        "SELECT updated_at, origin_device_id FROM playlist_memberships"
    ).fetchall()
    assert members and all(u and o for u, o in members)
    assert {o for _u, o in members} == {playlist[1]}, (
        "every row this importer writes carries THIS machine's origin"
    )

    counts = _changelog(state_conn)
    # Two playlists: the Spotify vendor playlist AND its linked ODJ twin (the
    # user-facing browser playlist), both stamped and offered to the hub.
    assert counts.get("playlists") == 2
    assert counts.get("playlist_memberships") == len(members)


def test_a_reimport_outranks_the_previous_import(state_conn: sqlite3.Connection):
    """The hub's test is ``incoming <= stored -> reject``, so a re-import
    that presents an equal key is a change that never propagates."""
    result = MatchResult(pairs=[
        MatchedPair(_mk_src("t1"), _mk_local("s1"), 1.0, (), "matched"),
    ])
    write_playlist_and_pending(
        state_conn, _mk_playlist(snapshot="snap-1", tracks=()), result,
        backup_path=_TEST_BACKUP, reversal_script_path=_TEST_REVERSAL,
    )
    first = state_conn.execute(
        "SELECT updated_at, origin_device_id FROM playlists"
    ).fetchone()

    write_playlist_and_pending(
        state_conn, _mk_playlist(snapshot="snap-2", tracks=()), result,
        backup_path=_TEST_BACKUP, reversal_script_path=_TEST_REVERSAL,
    )
    second = state_conn.execute(
        "SELECT updated_at, origin_device_id FROM playlists"
    ).fetchone()

    assert (second[0], second[1]) > (first[0], first[0])
    # Two imports x two playlists (Spotify + linked ODJ twin) = four rows.
    assert _changelog(state_conn).get("playlists") == 4


@pytest.mark.requirement("LIBM-140")
def test_a_reimport_leaves_a_removed_placeholder_removed(state_conn: sqlite3.Connection) -> None:
    """Re-importing a playlist is not a restore: the placeholder the user
    removed keeps its tombstone and stays out of the playlist, while the
    placeholder beside it (the control) is written as before."""
    removed_src = _mk_src(sid="t-removed", title="Removed")
    kept_src = _mk_src(sid="t-kept", title="Kept")
    result = MatchResult(pairs=[
        MatchedPair(removed_src, None, 0.0, (), "unmatched"),
        MatchedPair(kept_src, None, 0.0, (), "unmatched"),
    ])
    first = _write(state_conn, _mk_playlist(snapshot="snap-1", tracks=(removed_src, kept_src)), result)
    assert first.tracks_skipped_deleted == 0
    removed_sid = synthetic_stable_id(removed_src.spotify_uri)
    StateWriter(state_conn, actor="test").remove_from_library(removed_sid)
    tombstone = state_conn.execute(
        "SELECT deleted_at, updated_at FROM tracks WHERE stable_id = ?", (removed_sid,)
    ).fetchone()
    assert tombstone[0] is not None

    second = _write(state_conn, _mk_playlist(snapshot="snap-2", tracks=(removed_src, kept_src)), result)

    assert second.tracks_skipped_deleted == 1
    assert state_conn.execute(
        "SELECT deleted_at, updated_at FROM tracks WHERE stable_id = ?", (removed_sid,)
    ).fetchone() == tombstone, "a playlist re-import rewrote a removed track"
    live_members = {
        row[0]
        for row in state_conn.execute(
            "SELECT stable_id FROM playlist_memberships WHERE deleted_at IS NULL"
        )
    }
    assert removed_sid not in live_members
    assert synthetic_stable_id(kept_src.spotify_uri) in live_members
