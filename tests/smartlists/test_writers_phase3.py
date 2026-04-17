"""Tests for the Phase 3 <-> Phase 8 playlist-writer wrappers.

Covers both :class:`apps.smartlists.rb_writer.RBPlaylistWriter` (drives
pyrekordbox's ORM via duck-typed fake) and
:class:`apps.smartlists.djay_writer.DjayPlaylistWriter` (drives Phase 3
``_apply_single_op`` against a throwaway SQLite DB).

Target: >= 80% coverage on both wrappers + the stable_id resolver
helpers. Live vendor DBs are never touched.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from apps.smartlists.djay_writer import (
    DjayPlaylistWriter,
    _find_djay_playlist,
    _resolve_djay_uuid,
    build_djay_writer,
)
from apps.smartlists.rb_writer import (
    RBPlaylistWriter,
    _resolve_rb_id,
    build_rb_writer,
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


class _FakePlaylist:
    def __init__(self, name: str) -> None:
        self.Name = name
        self.members: list[str] = []


class _FakeRBDatabase:
    """Minimal duck-typed pyrekordbox stand-in for tests."""

    def __init__(self, initial: list[_FakePlaylist] | None = None) -> None:
        self._playlists: list[_FakePlaylist] = list(initial or [])
        self.commits = 0
        self.create_calls: list[str] = []
        self.add_calls: list[tuple[str, str]] = []
        self.remove_calls: list[tuple[str, str]] = []

    def get_playlist(self):
        return list(self._playlists)

    def create_playlist(self, name: str) -> _FakePlaylist:
        self.create_calls.append(name)
        pl = _FakePlaylist(name)
        self._playlists.append(pl)
        return pl

    def add_to_playlist(self, pl: _FakePlaylist, rb_id: str) -> None:
        self.add_calls.append((pl.Name, rb_id))
        pl.members.append(rb_id)

    def remove_from_playlist(self, pl: _FakePlaylist, rb_id: str) -> None:
        self.remove_calls.append((pl.Name, rb_id))
        if rb_id in pl.members:
            pl.members.remove(rb_id)

    def commit(self) -> None:
        self.commits += 1


class TestRBResolve:
    def test_resolves_when_mapping_exists(self, state_conn) -> None:
        assert _resolve_rb_id(state_conn, "sid-1") == "rb-100"
        assert _resolve_rb_id(state_conn, "sid-3") == "rb-300"

    def test_returns_none_for_unknown_stable_id(self, state_conn) -> None:
        assert _resolve_rb_id(state_conn, "no-such") is None

    def test_ignores_other_vendors(self, state_conn) -> None:
        # No rekordbox row for sid-foo even though djay has the sid.
        state_conn.execute(
            "INSERT INTO track_vendor_ids(stable_id, vendor, vendor_id) "
            "VALUES (?, ?, ?)",
            ("sid-foo", "djay", "dj-foo"),
        )
        assert _resolve_rb_id(state_conn, "sid-foo") is None


class TestRBPlaylistWriter:
    def test_playlist_exists(self, state_conn) -> None:
        db = _FakeRBDatabase([_FakePlaylist("[SL] Foo")])
        w = RBPlaylistWriter(db=db, state_conn=state_conn)
        assert w.playlist_exists("[SL] Foo") is True
        assert w.playlist_exists("[SL] Bar") is False
        assert w.vendor == "rekordbox"

    def test_create_playlist_resolves_and_adds(self, state_conn) -> None:
        db = _FakeRBDatabase()
        w = RBPlaylistWriter(db=db, state_conn=state_conn)
        w.create_playlist("[SL] New", ["sid-1", "sid-2"])
        assert db.create_calls == ["[SL] New"]
        assert db.add_calls == [
            ("[SL] New", "rb-100"),
            ("[SL] New", "rb-200"),
        ]
        assert db.commits == 1

    def test_create_playlist_raises_on_missing_mapping(
        self, state_conn
    ) -> None:
        db = _FakeRBDatabase()
        w = RBPlaylistWriter(db=db, state_conn=state_conn)
        with pytest.raises(RuntimeError, match="no ContentID mapping"):
            w.create_playlist("[SL] Bad", ["sid-1", "missing"])
        # No partial write.
        assert db.commits == 0

    def test_apply_diff_adds_and_removes(self, state_conn) -> None:
        pl = _FakePlaylist("[SL] Dance")
        pl.members = ["rb-100", "rb-200"]
        db = _FakeRBDatabase([pl])
        w = RBPlaylistWriter(db=db, state_conn=state_conn)
        w.apply_diff("[SL] Dance", added=["sid-3"], removed=["sid-1"])
        assert db.remove_calls == [("[SL] Dance", "rb-100")]
        assert db.add_calls == [("[SL] Dance", "rb-300")]
        assert pl.members == ["rb-200", "rb-300"]
        assert db.commits == 1

    def test_apply_diff_raises_if_playlist_missing(self, state_conn) -> None:
        db = _FakeRBDatabase()
        w = RBPlaylistWriter(db=db, state_conn=state_conn)
        with pytest.raises(RuntimeError, match="not found"):
            w.apply_diff("[SL] Missing", added=["sid-1"], removed=[])

    def test_build_rb_writer_returns_none_on_failure(self) -> None:
        def bad_db():
            raise RuntimeError("nope")

        def bad_state():
            raise RuntimeError("nope")

        # Either factory raising returns None.
        assert build_rb_writer(bad_db, bad_state) is None

    def test_build_rb_writer_succeeds_with_factories(
        self, state_conn
    ) -> None:
        db = _FakeRBDatabase()
        w = build_rb_writer(
            db_factory=lambda: db, state_conn_factory=lambda: state_conn
        )
        assert w is not None
        assert isinstance(w, RBPlaylistWriter)


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
    # Seed userdata rows so resolve_userdata_rowids in playlist_apply can
    # map djay_uuid -> rowid.
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


def _seed_existing_playlist(
    djay_db: Path, name: str, member_uuids: list[str]
) -> str:
    """Seed a playlist row + page(s). Returns the playlist uuid."""
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


class TestDjayResolve:
    def test_resolves_when_mapping_exists(self, state_conn) -> None:
        assert _resolve_djay_uuid(state_conn, "sid-1") == "dj-uuid-100"

    def test_returns_none_for_unknown(self, state_conn) -> None:
        assert _resolve_djay_uuid(state_conn, "nope") is None


class TestFindDjayPlaylist:
    def test_finds_by_name_in_blob(self, djay_db) -> None:
        uuid = _seed_existing_playlist(
            djay_db, "[SL] Hello", ["dj-uuid-100", "dj-uuid-200"]
        )
        con = sqlite3.connect(str(djay_db), isolation_level=None)
        try:
            found = _find_djay_playlist(con, "[SL] Hello")
        finally:
            con.close()
        assert found is not None
        assert found[0] == uuid
        assert len(found[1]) == 2

    def test_returns_none_when_missing(self, djay_db) -> None:
        con = sqlite3.connect(str(djay_db), isolation_level=None)
        try:
            assert _find_djay_playlist(con, "[SL] Nope") is None
        finally:
            con.close()


class TestDjayPlaylistWriter:
    def test_playlist_exists_true_false(self, djay_db, state_conn) -> None:
        _seed_existing_playlist(djay_db, "[SL] Seeded", ["dj-uuid-100"])
        w = DjayPlaylistWriter(djay_db_path=djay_db, state_conn=state_conn)
        assert w.playlist_exists("[SL] Seeded") is True
        assert w.playlist_exists("[SL] Nope") is False
        assert w.vendor == "djay"

    def test_create_playlist_end_to_end(
        self, djay_db, state_conn
    ) -> None:
        w = DjayPlaylistWriter(djay_db_path=djay_db, state_conn=state_conn)
        w.create_playlist("[SL] Fresh", ["sid-1", "sid-2"])
        # The playlist row + page row should now exist.
        con = sqlite3.connect(str(djay_db))
        try:
            pl_rows = con.execute(
                "SELECT key FROM database2 "
                "WHERE collection = 'mediaItemPlaylists'"
            ).fetchall()
            pages = con.execute(
                "SELECT data FROM view_mediaItemPlaylistView_page"
            ).fetchall()
        finally:
            con.close()
        assert len(pl_rows) == 1
        assert len(pages) == 1

    def test_create_raises_on_missing_mapping(
        self, djay_db, state_conn
    ) -> None:
        w = DjayPlaylistWriter(djay_db_path=djay_db, state_conn=state_conn)
        with pytest.raises(RuntimeError, match="no djay_uuid"):
            w.create_playlist("[SL] Bad", ["sid-1", "unknown-sid"])

    def test_apply_diff_updates_membership(
        self, djay_db, state_conn
    ) -> None:
        _seed_existing_playlist(
            djay_db, "[SL] Mix", ["dj-uuid-100", "dj-uuid-200"]
        )
        w = DjayPlaylistWriter(djay_db_path=djay_db, state_conn=state_conn)
        w.apply_diff("[SL] Mix", added=["sid-3"], removed=["sid-1"])
        # After the diff: removed sid-1 (dj-uuid-100), kept sid-2, added
        # sid-3. Verify by reading the page data back.
        con = sqlite3.connect(str(djay_db))
        try:
            found = _find_djay_playlist(con, "[SL] Mix")
            assert found is not None
            _uuid, rowids = found
            uuid_by_row = {
                int(r): k for r, k in con.execute(
                    "SELECT rowid, key FROM database2 "
                    "WHERE collection = 'mediaItemUserData'"
                )
            }
            actual = [uuid_by_row.get(r) for r in rowids]
        finally:
            con.close()
        assert "dj-uuid-100" not in actual
        assert "dj-uuid-200" in actual
        assert "dj-uuid-300" in actual

    def test_apply_diff_raises_if_missing(
        self, djay_db, state_conn
    ) -> None:
        w = DjayPlaylistWriter(djay_db_path=djay_db, state_conn=state_conn)
        with pytest.raises(RuntimeError, match="not found"):
            w.apply_diff("[SL] Ghost", added=["sid-1"], removed=[])

    def test_build_djay_writer_returns_none_without_db(
        self, tmp_path, state_conn
    ) -> None:
        # Path that doesn't exist -> None.
        assert build_djay_writer(
            tmp_path / "no-such.db", state_conn=state_conn
        ) is None

    def test_build_djay_writer_returns_writer(
        self, djay_db, state_conn
    ) -> None:
        w = build_djay_writer(djay_db, state_conn=state_conn)
        assert w is not None
        assert isinstance(w, DjayPlaylistWriter)


# --------------------------------------------------------- refresh wiring


class TestRefreshBuildWriters:
    def test_build_writers_filters_none(self, monkeypatch) -> None:
        """``_build_writers`` drops factories that return None."""
        from apps.smartlists import refresh

        monkeypatch.setattr(
            "apps.smartlists.rb_writer.build_rb_writer",
            lambda: None,
        )
        monkeypatch.setattr(
            "apps.smartlists.djay_writer.build_djay_writer",
            lambda: None,
        )
        assert refresh._build_writers() == []

    def test_build_writers_swallows_exceptions(self, monkeypatch) -> None:
        from apps.smartlists import refresh

        def boom():
            raise RuntimeError("nope")

        monkeypatch.setattr(
            "apps.smartlists.rb_writer.build_rb_writer", boom,
        )
        monkeypatch.setattr(
            "apps.smartlists.djay_writer.build_djay_writer", boom,
        )
        assert refresh._build_writers() == []

    def test_build_writers_keeps_live_writers(
        self, monkeypatch, djay_db, state_conn
    ) -> None:
        from apps.smartlists import refresh

        dj_writer = DjayPlaylistWriter(
            djay_db_path=djay_db, state_conn=state_conn
        )
        monkeypatch.setattr(
            "apps.smartlists.rb_writer.build_rb_writer",
            lambda: None,
        )
        monkeypatch.setattr(
            "apps.smartlists.djay_writer.build_djay_writer",
            lambda: dj_writer,
        )
        writers = refresh._build_writers()
        assert writers == [dj_writer]
