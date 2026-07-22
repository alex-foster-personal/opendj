"""Tests for apps.spotify.state_writer."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from apps.shared.state import schema as state_schema
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
    mark_pending_abandoned,
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
        "SELECT stable_id, position FROM playlist_memberships"
    ).fetchall()
    assert rows == [("s1", 0)]

    assert already_imported_snapshot(state_conn, "pl123") == "snap-1"


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
    assert str(backup) in text
    assert str(target) in text


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
