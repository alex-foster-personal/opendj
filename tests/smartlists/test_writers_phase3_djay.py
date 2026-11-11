"""Djay playlist-writer and refresh factory cases for the Phase 3 wrappers.

[if] a djay playlist writer or refresh factory runs [then] it creates, diffs, or filters writers as specified, [else stop].

Split from ``test_writers_phase3.py``: djay uuid resolve, playlist create
and diff, and refresh._build_writers wiring.
"""

from __future__ import annotations

import sqlite3

import pytest

from apps.smartlists.djay_writer import (
    DjayPlaylistWriter,
    _find_djay_playlist,
    _resolve_djay_uuid,
    build_djay_writer,
)
from tests.smartlists import writers_phase3_support as _support
from tests.smartlists.writers_phase3_support import _seed_existing_playlist

djay_db = _support.djay_db
state_conn = _support.state_conn

# Live-write MECHANICS against tmp fixtures: runs with the one-way rekordbox
# import gate ON (root conftest reads the marker). Never a real rb target.
pytestmark = [pytest.mark.requirement("SMART-02"), pytest.mark.rekordbox_writeback]


# ------------------------------------------------------------ djay_writer


class TestDjayResolve:
    def test_resolves_when_mapping_exists(self, state_conn) -> None:
        assert _resolve_djay_uuid(state_conn, "sid-1") == "dj-uuid-100"

    def test_returns_none_for_unknown(self, state_conn) -> None:
        assert _resolve_djay_uuid(state_conn, "nope") is None


class TestFindDjayPlaylist:
    def test_finds_by_name_in_blob(self, djay_db) -> None:
        uuid = _seed_existing_playlist(djay_db, "[SL] Hello", ["dj-uuid-100", "dj-uuid-200"])
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

    def test_create_playlist_end_to_end(self, djay_db, state_conn) -> None:
        w = DjayPlaylistWriter(djay_db_path=djay_db, state_conn=state_conn)
        w.create_playlist("[SL] Fresh", ["sid-1", "sid-2"])
        # The playlist row + page row should now exist.
        con = sqlite3.connect(str(djay_db))
        try:
            pl_rows = con.execute("SELECT key FROM database2 WHERE collection = 'mediaItemPlaylists'").fetchall()
            pages = con.execute("SELECT data FROM view_mediaItemPlaylistView_page").fetchall()
        finally:
            con.close()
        assert len(pl_rows) == 1
        assert len(pages) == 1

    def test_create_raises_on_missing_mapping(self, djay_db, state_conn) -> None:
        w = DjayPlaylistWriter(djay_db_path=djay_db, state_conn=state_conn)
        with pytest.raises(RuntimeError, match="no djay_uuid"):
            w.create_playlist("[SL] Bad", ["sid-1", "unknown-sid"])

    def test_apply_diff_updates_membership(self, djay_db, state_conn) -> None:
        _seed_existing_playlist(djay_db, "[SL] Mix", ["dj-uuid-100", "dj-uuid-200"])
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
                int(r): k
                for r, k in con.execute("SELECT rowid, key FROM database2 WHERE collection = 'mediaItemUserData'")
            }
            actual = [uuid_by_row.get(r) for r in rowids]
        finally:
            con.close()
        assert "dj-uuid-100" not in actual
        assert "dj-uuid-200" in actual
        assert "dj-uuid-300" in actual

    def test_apply_diff_raises_if_missing(self, djay_db, state_conn) -> None:
        w = DjayPlaylistWriter(djay_db_path=djay_db, state_conn=state_conn)
        with pytest.raises(RuntimeError, match="not found"):
            w.apply_diff("[SL] Ghost", added=["sid-1"], removed=[])

    def test_build_djay_writer_returns_none_without_db(self, tmp_path, state_conn) -> None:
        # Path that doesn't exist -> None.
        assert build_djay_writer(tmp_path / "no-such.db", state_conn=state_conn) is None

    def test_build_djay_writer_returns_writer(self, djay_db, state_conn) -> None:
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
            lambda **kw: None,
        )
        monkeypatch.setattr(
            "apps.smartlists.djay_writer.build_djay_writer",
            lambda: None,
        )
        assert refresh._build_writers() == []

    def test_build_writers_swallows_exceptions(self, monkeypatch) -> None:
        from apps.smartlists import refresh

        def boom(*_a, **_kw):
            raise RuntimeError("nope")

        monkeypatch.setattr(
            "apps.smartlists.rb_writer.build_rb_writer",
            boom,
        )
        monkeypatch.setattr(
            "apps.smartlists.djay_writer.build_djay_writer",
            boom,
        )
        assert refresh._build_writers() == []

    def test_build_writers_keeps_live_writers(self, monkeypatch, djay_db, state_conn) -> None:
        from apps.smartlists import refresh

        dj_writer = DjayPlaylistWriter(djay_db_path=djay_db, state_conn=state_conn)
        monkeypatch.setattr(
            "apps.smartlists.rb_writer.build_rb_writer",
            lambda **kw: None,
        )
        monkeypatch.setattr(
            "apps.smartlists.djay_writer.build_djay_writer",
            lambda: dj_writer,
        )
        writers = refresh._build_writers()
        assert writers == [dj_writer]

    def test_build_writers_forwards_live_to_rb_factory(self, monkeypatch) -> None:
        """``_build_writers(live=True)`` must propagate to the RB factory."""
        from apps.smartlists import refresh

        seen: dict = {}

        def fake_build(**kwargs):
            seen.update(kwargs)

        monkeypatch.setattr(
            "apps.smartlists.rb_writer.build_rb_writer",
            fake_build,
        )
        monkeypatch.setattr(
            "apps.smartlists.djay_writer.build_djay_writer",
            lambda: None,
        )
        refresh._build_writers(live=True)
        assert seen == {"live": True}
