"""Tests for ``read_members`` on the Phase 3 <-> Phase 8 playlist-writer
wrappers -- the "old side of the diff" hook the write-back-rekordbox-djay
feature (LANE playlists-router) adds so playlist writeback can diff
against LIVE vendor state without a persisted materialisation record.

Mirrors the fixtures in ``tests/smartlists/test_writers_phase3.py`` (same
duck-typed RB database / real throwaway djay sqlite file) so the two
suites stay easy to cross-reference.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from apps.smartlists.djay_writer import (
    DjayPlaylistWriter,
    _find_djay_playlist_by_id,
    _resolve_stable_id_for_djay,
)
from apps.smartlists.rb_writer import (
    RBPlaylistWriter,
    _online_backup_unlocked_rekordbox,
    _resolve_stable_id_for_rb,
)


# ---------------------------------------------------------------- state DB

_STATE_DDL = """
CREATE TABLE track_vendor_ids (
    stable_id TEXT NOT NULL,
    vendor TEXT NOT NULL,
    vendor_id TEXT NOT NULL,
    PRIMARY KEY (stable_id, vendor)
);
"""


@pytest.fixture
def state_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.executescript(_STATE_DDL)
    rows = [
        ("sid-1", "rekordbox", "rb-100"),
        ("sid-2", "rekordbox", "rb-200"),
        ("sid-3", "rekordbox", "rb-300"),
        ("sid-1", "djay", "dj-uuid-100"),
        ("sid-2", "djay", "dj-uuid-200"),
        ("sid-3", "djay", "dj-uuid-300"),
    ]
    conn.executemany(
        "INSERT INTO track_vendor_ids(stable_id, vendor, vendor_id) "
        "VALUES (?, ?, ?)",
        rows,
    )
    yield conn
    conn.close()


# --------------------------------------------------------------- rb_writer


class _FakeSong:
    def __init__(self, content_id: str, track_no: int) -> None:
        self.ContentID = content_id
        self.TrackNo = track_no


class _FakePlaylist:
    def __init__(self, name: str, songs: list[_FakeSong] | None = None) -> None:
        self.Name = name
        self.Songs = list(songs or [])


class _FakeRBDatabase:
    def __init__(self, playlists: list[_FakePlaylist] | None = None) -> None:
        self._playlists = list(playlists or [])

    def get_playlist(self):
        return list(self._playlists)


class TestResolveStableIdForRB:
    def test_resolves_when_mapping_exists(self, state_conn) -> None:
        assert _resolve_stable_id_for_rb(state_conn, "rb-100") == "sid-1"

    def test_returns_none_for_unknown_content_id(self, state_conn) -> None:
        assert _resolve_stable_id_for_rb(state_conn, "no-such") is None


class TestRBReadMembers:
    def test_returns_stable_ids_in_track_no_order(self, state_conn) -> None:
        pl = _FakePlaylist(
            "My Set",
            [
                _FakeSong("rb-300", track_no=2),
                _FakeSong("rb-100", track_no=1),
            ],
        )
        db = _FakeRBDatabase([pl])
        w = RBPlaylistWriter(db=db, state_conn=state_conn)
        assert w.read_members("My Set") == ["sid-1", "sid-3"]

    def test_skips_unmapped_content_ids(self, state_conn) -> None:
        pl = _FakePlaylist(
            "My Set",
            [
                _FakeSong("rb-100", track_no=1),
                _FakeSong("rb-untracked", track_no=2),
            ],
        )
        db = _FakeRBDatabase([pl])
        w = RBPlaylistWriter(db=db, state_conn=state_conn)
        assert w.read_members("My Set") == ["sid-1"]

    def test_empty_playlist_returns_empty_list(self, state_conn) -> None:
        db = _FakeRBDatabase([_FakePlaylist("Empty")])
        w = RBPlaylistWriter(db=db, state_conn=state_conn)
        assert w.read_members("Empty") == []

    def test_raises_when_playlist_missing(self, state_conn) -> None:
        db = _FakeRBDatabase([])
        w = RBPlaylistWriter(db=db, state_conn=state_conn)
        with pytest.raises(RuntimeError, match="not found"):
            w.read_members("Ghost")


_SQLCIPHER_KEY = "pr273-test-key"
_PYREKORDBOX_TEST_KEY = (
    "402fd0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
)


def _create_encrypted_marker_db(path: Path) -> None:
    import sqlcipher3

    connection = sqlcipher3.connect(path)
    connection.execute(f"PRAGMA key = '{_SQLCIPHER_KEY}'")
    connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
    connection.execute("INSERT INTO marker VALUES ('encrypted-rekordbox')")
    connection.commit()
    connection.close()

    with sqlite3.connect(path) as locked:
        with pytest.raises(sqlite3.DatabaseError, match="not a database"):
            locked.execute("SELECT value FROM marker").fetchone()


def _open_sqlcipher_database(path: Path) -> SimpleNamespace:
    import sqlcipher3

    engine = create_engine(
        f"sqlite+pysqlcipher://:{_SQLCIPHER_KEY}@/{path}?",
        module=sqlcipher3,
    )
    return SimpleNamespace(engine=engine, session=Session(engine))


def _close_sqlcipher_database(database: SimpleNamespace) -> None:
    database.session.close()
    database.engine.dispose()


def _encrypt_rekordbox_copy(source: Path, destination: Path) -> None:
    import sqlcipher3

    connection = sqlcipher3.connect(source)
    try:
        connection.execute(
            f"ATTACH DATABASE ? AS encrypted KEY '{_PYREKORDBOX_TEST_KEY}'",
            (str(destination),),
        )
        connection.execute("SELECT sqlcipher_export('encrypted')").fetchone()
        connection.commit()
        connection.execute("DETACH DATABASE encrypted")
    finally:
        connection.close()


def test_online_backup_preserves_real_sqlcipher_session_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlcipher3

    from apps.smartlists import writeback_backup

    encrypted = tmp_path / "master.db"
    _create_encrypted_marker_db(encrypted)
    database = _open_sqlcipher_database(encrypted)
    session = database.session
    monkeypatch.setattr(writeback_backup, "WRITEBACK_BACKUP_DIR", tmp_path / "backups")

    try:
        assert type(database.engine.pool).__name__ == "SingletonThreadPool"
        session.execute(text("BEGIN IMMEDIATE"))
        session_driver = session.connection().connection.driver_connection
        assert session_driver.in_transaction

        _online_backup_unlocked_rekordbox(database, encrypted)

        assert session_driver.in_transaction
        contender = sqlcipher3.connect(encrypted, timeout=0, isolation_level=None)
        try:
            contender.execute(f"PRAGMA key = '{_SQLCIPHER_KEY}'")
            with pytest.raises(sqlcipher3.OperationalError, match="locked"):
                contender.execute("BEGIN IMMEDIATE")
        finally:
            contender.close()
    finally:
        session.rollback()
        _close_sqlcipher_database(database)


def test_online_backup_uses_unlocked_sqlcipher_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.smartlists import writeback_backup

    encrypted = tmp_path / "master.db"
    _create_encrypted_marker_db(encrypted)
    database = _open_sqlcipher_database(encrypted)
    monkeypatch.setattr(writeback_backup, "WRITEBACK_BACKUP_DIR", tmp_path / "backups")

    try:
        backup_id = _online_backup_unlocked_rekordbox(database, encrypted)
    finally:
        _close_sqlcipher_database(database)

    backup = writeback_backup.backup_path("rekordbox", backup_id)
    with sqlite3.connect(backup) as plain:
        assert plain.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert plain.execute("SELECT value FROM marker").fetchone() == (
            "encrypted-rekordbox",
        )


def test_online_backup_rejects_unlocked_connection_for_another_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.smartlists import writeback_backup

    encrypted = tmp_path / "master.db"
    _create_encrypted_marker_db(encrypted)
    database = _open_sqlcipher_database(encrypted)
    other_target = tmp_path / "other.db"
    other_target.write_bytes(encrypted.read_bytes())
    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(writeback_backup, "WRITEBACK_BACKUP_DIR", backup_dir)

    try:
        with pytest.raises(RuntimeError, match="does not own exact target"):
            _online_backup_unlocked_rekordbox(database, other_target)
    finally:
        _close_sqlcipher_database(database)

    assert not backup_dir.exists() or list(backup_dir.iterdir()) == []


def test_encrypted_rekordbox_apply_backup_and_rollback_round_trip(
    tmp_rb_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pyrekordbox import Rekordbox6Database

    from apps.smartlists import writeback_backup

    encrypted = tmp_path / "master.db"
    _encrypt_rekordbox_copy(tmp_rb_db, encrypted)
    database = Rekordbox6Database(
        path=str(encrypted),
        key=_PYREKORDBOX_TEST_KEY,
    )
    mapping_conn = sqlite3.connect(":memory:", isolation_level=None)
    mapping_conn.executescript(_STATE_DDL)
    monkeypatch.setattr(writeback_backup, "WRITEBACK_BACKUP_DIR", tmp_path / "backups")

    try:
        playlist = next(
            candidate
            for candidate in database.get_playlist()
            if list(getattr(candidate, "Songs", []) or [])
        )
        playlist_id = str(playlist.ID)
        songs = sorted(
            list(playlist.Songs),
            key=lambda song: (getattr(song, "TrackNo", 0) or 0),
        )
        native_members = [str(song.ContentID) for song in songs]
        stable_members = [f"stable-{index}" for index in range(len(native_members))]
        mapping_conn.executemany(
            "INSERT INTO track_vendor_ids VALUES (?, 'rekordbox', ?)",
            list(zip(stable_members, native_members, strict=True)),
        )
        desired_members = list(reversed(stable_members))
        expected_revision = hashlib.sha256(json.dumps(
            {"target_id": playlist_id, "members": stable_members},
            sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        mapping_revision = hashlib.sha256(json.dumps(sorted(
            zip(stable_members, native_members, strict=True)
        ), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        writer = RBPlaylistWriter(
            db=database,
            state_conn=mapping_conn,
            live_db_path=encrypted,
        )

        backup, post_revision = writer.apply_with_backup_by_id(
            playlist_id,
            desired_members,
            expected_revision,
            mapping_revision,
            nullcontext,
        )

        assert writer.read_members_by_id(playlist_id) == desired_members
        backup_path = writeback_backup.backup_path("rekordbox", backup.backup_id)
        with sqlite3.connect(backup_path) as plain_backup:
            assert plain_backup.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert writer.restore_backup(
            backup.backup_id,
            playlist_id,
            post_revision,
        ) == expected_revision
        assert writer.read_members_by_id(playlist_id) == stable_members
    finally:
        mapping_conn.close()
        database.close()


# ------------------------------------------------------------ djay_writer

_DJAY_DDL = """
CREATE TABLE database2 (
    collection TEXT NOT NULL,
    key TEXT NOT NULL,
    data BLOB,
    PRIMARY KEY (collection, key)
);
CREATE TABLE view_mediaItemPlaylistView_page (
    pageKey TEXT PRIMARY KEY,
    "group" TEXT NOT NULL,
    prevPageKey TEXT,
    count INTEGER,
    data BLOB
);
"""


@pytest.fixture
def djay_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "djay.db"
    con = sqlite3.connect(str(db_path), isolation_level=None)
    con.executescript(_DJAY_DDL)
    user_rows = [
        ("dj-uuid-100", b"blob-100"),
        ("dj-uuid-200", b"blob-200"),
        ("dj-uuid-300", b"blob-300"),
    ]
    con.executemany(
        "INSERT INTO database2(collection, key, data) VALUES "
        "('mediaItemUserData', ?, ?)",
        user_rows,
    )
    con.close()
    return db_path


def _seed_existing_playlist(djay_db: Path, name: str, member_uuids: list[str]) -> str:
    import uuid as _uuid

    from apps.sync import playlist_tsaf as ptsaf
    from apps.sync.playlist_apply import resolve_userdata_rowids

    uuid = _uuid.uuid4().hex
    blob = ptsaf.build_playlist_blob(uuid, name, kind="leaf")
    con = sqlite3.connect(str(djay_db), isolation_level=None)
    try:
        con.execute(
            "INSERT INTO database2(collection, key, data) "
            "VALUES ('mediaItemPlaylists', ?, ?)",
            (uuid, blob),
        )
        rowids = resolve_userdata_rowids(con, member_uuids)
        page_key = ptsaf.new_page_key()
        page_data = ptsaf.build_page_data(rowids)
        con.execute(
            'INSERT INTO view_mediaItemPlaylistView_page '
            '(pageKey, "group", prevPageKey, count, data) '
            "VALUES (?, ?, NULL, ?, ?)",
            (page_key, uuid, len(rowids), page_data),
        )
    finally:
        con.close()
    return uuid


class TestResolveStableIdForDjay:
    def test_resolves_when_mapping_exists(self, state_conn) -> None:
        assert _resolve_stable_id_for_djay(state_conn, "dj-uuid-200") == "sid-2"

    def test_returns_none_for_unknown_uuid(self, state_conn) -> None:
        assert _resolve_stable_id_for_djay(state_conn, "nope") is None


class TestDjayReadMembers:
    def test_returns_stable_ids_in_page_order(self, djay_db, state_conn) -> None:
        _seed_existing_playlist(
            djay_db, "My Set", ["dj-uuid-200", "dj-uuid-100"],
        )
        w = DjayPlaylistWriter(djay_db_path=djay_db, state_conn=state_conn)
        assert w.read_members("My Set") == ["sid-2", "sid-1"]

    def test_skips_unmapped_uuids(self, djay_db, state_conn) -> None:
        con = sqlite3.connect(str(djay_db), isolation_level=None)
        con.execute(
            "INSERT INTO database2(collection, key, data) VALUES "
            "('mediaItemUserData', 'dj-untracked', ?)",
            (b"blob-untracked",),
        )
        con.close()
        _seed_existing_playlist(
            djay_db, "My Set", ["dj-uuid-100", "dj-untracked"],
        )
        w = DjayPlaylistWriter(djay_db_path=djay_db, state_conn=state_conn)
        assert w.read_members("My Set") == ["sid-1"]

    def test_raises_when_playlist_missing(self, djay_db, state_conn) -> None:
        w = DjayPlaylistWriter(djay_db_path=djay_db, state_conn=state_conn)
        with pytest.raises(RuntimeError, match="not found"):
            w.read_members("Ghost")

    def test_native_id_read_refuses_an_unmapped_member_before_writeback(self, djay_db, state_conn) -> None:
        con = sqlite3.connect(str(djay_db), isolation_level=None)
        con.execute("INSERT INTO database2(collection, key, data) VALUES ('mediaItemUserData', 'dj-unmapped', ?)", (b"blob",))
        con.close()
        playlist_id = _seed_existing_playlist(djay_db, "Set", ["dj-uuid-100", "dj-unmapped"])
        writer = DjayPlaylistWriter(djay_db_path=djay_db, state_conn=state_conn)
        with pytest.raises(RuntimeError, match="unmapped UUID"):
            writer.read_members_by_id(playlist_id)

    def test_reversal_persistence_failure_rolls_back_without_mutating_target(self, djay_db, state_conn, monkeypatch, tmp_path) -> None:
        from apps.smartlists import writeback_backup

        playlist_id = _seed_existing_playlist(djay_db, "Set", ["dj-uuid-100"])
        writer = DjayPlaylistWriter(djay_db_path=djay_db, state_conn=state_conn, safety_session=object())
        expected = hashlib.sha256(json.dumps(
            {"target_id": playlist_id, "members": ["sid-1"]}, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        mapping_revision = hashlib.sha256(json.dumps(sorted([
            ("sid-1", "dj-uuid-100"), ("sid-2", "dj-uuid-200"),
        ]), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        monkeypatch.setattr(writeback_backup, "WRITEBACK_BACKUP_DIR", tmp_path / "backups")
        monkeypatch.setattr(writeback_backup, "write_reversal", lambda *_args: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(OSError, match="disk full"):
            writer.apply_with_backup_by_id(playlist_id, ["sid-1", "sid-2"], expected, mapping_revision, nullcontext)
        assert writer.read_members_by_id(playlist_id) == ["sid-1"]

    def test_rollback_uses_stable_cas_but_restores_exact_native_preimage(self, djay_db, state_conn, monkeypatch, tmp_path) -> None:
        from apps.smartlists import writeback_backup

        playlist_id = _seed_existing_playlist(djay_db, "Set", ["dj-uuid-100"])
        writer = DjayPlaylistWriter(djay_db_path=djay_db, state_conn=state_conn, safety_session=object())
        expected = hashlib.sha256(json.dumps(
            {"target_id": playlist_id, "members": ["sid-1"]}, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        mapping_revision = hashlib.sha256(json.dumps(sorted([
            ("sid-1", "dj-uuid-100"), ("sid-2", "dj-uuid-200"),
        ]), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        monkeypatch.setattr(writeback_backup, "WRITEBACK_BACKUP_DIR", tmp_path / "backups")

        backup, post_revision = writer.apply_with_backup_by_id(
            playlist_id, ["sid-1", "sid-2"], expected, mapping_revision, nullcontext,
        )

        assert post_revision == hashlib.sha256(json.dumps(
            {"target_id": playlist_id, "members": ["sid-1", "sid-2"]}, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        assert writer.restore_backup(backup.backup_id, playlist_id, post_revision) == expected

        con = sqlite3.connect(djay_db)
        try:
            _id, rowids = _find_djay_playlist_by_id(con, playlist_id) or pytest.fail("playlist missing")
            row_to_uuid = dict(con.execute("SELECT rowid, key FROM database2 WHERE collection = 'mediaItemUserData'"))
            assert [row_to_uuid[rowid] for rowid in rowids] == ["dj-uuid-100"]
        finally:
            con.close()
