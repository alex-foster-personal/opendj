"""Rekordbox playlist-writer cases for the Phase 3 wrappers.

[if] an RB playlist writer resolves ids or applies a live rail [then] it writes, backs up, or refuses as specified, [else stop].

Split from ``test_writers_phase3.py``: RB id resolve, playlist create and
diff, live rails, and master.db backup.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.smartlists.rb_writer import (
    RBPlaylistWriter,
    _backup_rb_db,
    _resolve_rb_id,
    build_rb_writer,
)
from tests.smartlists import writers_phase3_support as _support
from tests.smartlists.writers_phase3_support import (
    _FakePlaylist,
    _FakeRBDatabase,
)

state_conn = _support.state_conn

# Live-write MECHANICS against tmp fixtures: runs with the one-way rekordbox
# import gate ON (root conftest reads the marker). Never a real rb target.
pytestmark = [pytest.mark.requirement("SMART-02"), pytest.mark.rekordbox_writeback]


# --------------------------------------------------------------- rb_writer


class TestRBResolve:
    def test_resolves_when_mapping_exists(self, state_conn) -> None:
        assert _resolve_rb_id(state_conn, "sid-1") == "rb-100"
        assert _resolve_rb_id(state_conn, "sid-3") == "rb-300"

    def test_returns_none_for_unknown_stable_id(self, state_conn) -> None:
        assert _resolve_rb_id(state_conn, "no-such") is None

    def test_ignores_other_vendors(self, state_conn) -> None:
        # No rekordbox row for sid-foo even though djay has the sid.
        state_conn.execute(
            "INSERT INTO track_vendor_ids(stable_id, vendor, vendor_id) VALUES (?, ?, ?)",
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

    def test_create_playlist_raises_on_missing_mapping(self, state_conn) -> None:
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

    def test_build_rb_writer_succeeds_with_factories(self, state_conn) -> None:
        db = _FakeRBDatabase()
        w = build_rb_writer(db_factory=lambda: db, state_conn_factory=lambda: state_conn)
        assert w is not None
        assert isinstance(w, RBPlaylistWriter)
        # Default is non-live so the pgrep + backup rails are off.
        assert w.live is False

    def test_build_rb_writer_live_flag_propagates(self, state_conn) -> None:
        db = _FakeRBDatabase()
        w = build_rb_writer(
            db_factory=lambda: db,
            state_conn_factory=lambda: state_conn,
            live=True,
        )
        assert w is not None
        assert w.live is True


class TestRBPlaylistWriterLiveRails:
    """Phase 1 six-rail coverage for ``RBPlaylistWriter`` live writes."""

    def _make_writer(
        self,
        state_conn,
        tmp_path: Path,
        db: _FakeRBDatabase,
    ) -> RBPlaylistWriter:
        # Seed a non-empty master.db so the backup helper can copy it.
        live_db = tmp_path / "master.db"
        live_db.write_bytes(b"SQLite fake 0123456789")
        backup_dir = tmp_path / "backups"
        return RBPlaylistWriter(
            db=db,
            state_conn=state_conn,
            live=True,
            live_db_path=live_db,
            backup_dir=backup_dir,
        )

    def test_create_playlist_refuses_when_rekordbox_running(
        self,
        state_conn,
        tmp_path,
        monkeypatch,
    ) -> None:
        db = _FakeRBDatabase()
        w = self._make_writer(state_conn, tmp_path, db)
        monkeypatch.setattr(
            "apps.smartlists.rb_writer._rekordbox_running",
            lambda: True,
        )
        with pytest.raises(RuntimeError, match="Rekordbox appears to be running"):
            w.create_playlist("[SL] Blocked", ["sid-1"])
        # No partial write: the fake DB never saw a commit.
        assert db.commits == 0
        assert db.create_calls == []

    def test_apply_diff_refuses_when_rekordbox_running(
        self,
        state_conn,
        tmp_path,
        monkeypatch,
    ) -> None:
        pl = _FakePlaylist("[SL] Dance")
        pl.members = ["rb-100"]
        db = _FakeRBDatabase([pl])
        w = self._make_writer(state_conn, tmp_path, db)
        monkeypatch.setattr(
            "apps.smartlists.rb_writer._rekordbox_running",
            lambda: True,
        )
        with pytest.raises(RuntimeError, match="Rekordbox appears to be running"):
            w.apply_diff("[SL] Dance", added=["sid-2"], removed=[])
        assert db.commits == 0
        assert db.add_calls == []

    def test_create_playlist_takes_backup_once_then_reuses(
        self,
        state_conn,
        tmp_path,
        monkeypatch,
    ) -> None:
        db = _FakeRBDatabase()
        w = self._make_writer(state_conn, tmp_path, db)
        monkeypatch.setattr(
            "apps.smartlists.rb_writer._rekordbox_running",
            lambda: False,
        )
        w.create_playlist("[SL] First", ["sid-1"])
        backup_dir = w.backup_dir
        first_backups = sorted(backup_dir.glob("master.*.smartlists.db"))
        assert len(first_backups) == 1
        assert first_backups[0].stat().st_size > 0
        # A second op on the same writer must not take a second backup.
        w.apply_diff("[SL] First", added=["sid-2"], removed=[])
        second_backups = sorted(backup_dir.glob("master.*.smartlists.db"))
        assert second_backups == first_backups
        assert db.commits == 2

    def test_live_false_bypasses_rails(
        self,
        state_conn,
        tmp_path,
        monkeypatch,
    ) -> None:
        """Unit tests that don't care about rails see no change."""
        db = _FakeRBDatabase()
        # Note: live defaults to False; pgrep + backup must not run even
        # if the helpers would raise. We assert by pointing the backup
        # path at a file that does not exist; if the rail fired it would
        # raise FileNotFoundError inside shutil.copy2.
        w = RBPlaylistWriter(
            db=db,
            state_conn=state_conn,
            live=False,
            live_db_path=tmp_path / "does-not-exist.db",
            backup_dir=tmp_path / "backups",
        )
        monkeypatch.setattr(
            "apps.smartlists.rb_writer._rekordbox_running",
            lambda: (_ for _ in ()).throw(AssertionError("should not be called")),
        )
        w.create_playlist("[SL] Safe", ["sid-1"])
        assert db.commits == 1


class TestBackupRBDB:
    def test_backup_copies_and_timestamps(self, tmp_path) -> None:
        src = tmp_path / "master.db"
        src.write_bytes(b"hello world")
        backup_dir = tmp_path / "backups"
        dst = _backup_rb_db(live_db=src, backup_dir=backup_dir)
        assert dst.parent == backup_dir
        assert dst.name.startswith("master.")
        assert dst.name.endswith(".smartlists.db")
        assert dst.read_bytes() == b"hello world"

    def test_backup_raises_on_empty_copy(self, tmp_path) -> None:
        src = tmp_path / "master.db"
        src.write_bytes(b"")
        backup_dir = tmp_path / "backups"
        with pytest.raises(RuntimeError, match="backup failed"):
            _backup_rb_db(live_db=src, backup_dir=backup_dir)
