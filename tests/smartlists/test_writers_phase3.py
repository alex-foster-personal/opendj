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
    _backup_rb_db,
    _resolve_rb_id,
    build_rb_writer,
)

try:
    from apps.smartlists.writers import (
        DEFAULT_CONFIRM_PHRASE,
        FakeWriter,
        SafePlaylistWriter,
        require_typed_confirm_phrase,
        safe_writer_session,
    )
except ImportError:  # pragma: no cover - WIP six-rail adapter, not yet merged
    pytest.skip(
        "apps.smartlists.writers does not yet export the six-rail adapter "
        "symbols; skipping until the Phase 3/8 SafePlaylistWriter work is "
        "committed. Tracked via CI watcher.",
        allow_module_level=True,
    )
from apps.sync.safety import SafetyAbort

# Live-write MECHANICS against tmp fixtures: runs with the one-way rekordbox
# import gate ON (root conftest reads the marker). Never a real rb target.
pytestmark = [pytest.mark.requirement("SMART-02"), pytest.mark.rekordbox_writeback]


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
        self, state_conn, tmp_path: Path, db: _FakeRBDatabase,
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
        self, state_conn, tmp_path, monkeypatch,
    ) -> None:
        db = _FakeRBDatabase()
        w = self._make_writer(state_conn, tmp_path, db)
        monkeypatch.setattr(
            "apps.smartlists.rb_writer._rekordbox_running", lambda: True,
        )
        with pytest.raises(RuntimeError, match="Rekordbox appears to be running"):
            w.create_playlist("[SL] Blocked", ["sid-1"])
        # No partial write: the fake DB never saw a commit.
        assert db.commits == 0
        assert db.create_calls == []

    def test_apply_diff_refuses_when_rekordbox_running(
        self, state_conn, tmp_path, monkeypatch,
    ) -> None:
        pl = _FakePlaylist("[SL] Dance")
        pl.members = ["rb-100"]
        db = _FakeRBDatabase([pl])
        w = self._make_writer(state_conn, tmp_path, db)
        monkeypatch.setattr(
            "apps.smartlists.rb_writer._rekordbox_running", lambda: True,
        )
        with pytest.raises(RuntimeError, match="Rekordbox appears to be running"):
            w.apply_diff("[SL] Dance", added=["sid-2"], removed=[])
        assert db.commits == 0
        assert db.add_calls == []

    def test_create_playlist_takes_backup_once_then_reuses(
        self, state_conn, tmp_path, monkeypatch,
    ) -> None:
        db = _FakeRBDatabase()
        w = self._make_writer(state_conn, tmp_path, db)
        monkeypatch.setattr(
            "apps.smartlists.rb_writer._rekordbox_running", lambda: False,
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
        self, state_conn, tmp_path, monkeypatch,
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
            lambda **kw: None,
        )
        monkeypatch.setattr(
            "apps.smartlists.djay_writer.build_djay_writer",
            lambda: dj_writer,
        )
        writers = refresh._build_writers()
        assert writers == [dj_writer]

    def test_build_writers_forwards_live_to_rb_factory(
        self, monkeypatch
    ) -> None:
        """``_build_writers(live=True)`` must propagate to the RB factory."""
        from apps.smartlists import refresh

        seen: dict = {}

        def fake_build(**kwargs):
            seen.update(kwargs)
            return None

        monkeypatch.setattr(
            "apps.smartlists.rb_writer.build_rb_writer", fake_build,
        )
        monkeypatch.setattr(
            "apps.smartlists.djay_writer.build_djay_writer",
            lambda: None,
        )
        refresh._build_writers(live=True)
        assert seen == {"live": True}


# -------------------------------------------------------- SafePlaylistWriter
#
# Six-rail coverage for apps.smartlists.writers.SafePlaylistWriter.
# Each test exercises one or more of:
#   rail 1: pgrep process gate
#   rail 2: timestamped DB backup
#   rail 3: typed confirmation (flag_ok + prompt helper)
#   rail 4: dry-run default
#   rail 5: post-write verify (verifier callback)
#   rail 6: reversal script emitted into data/sync/reversal/<ts>
# All tests use a fixture DB under tmp_path -- the live master.db /
# MediaLibrary.db is never touched.


def _make_safe_writer(
    tmp_path: Path,
    *,
    dry_run: bool = True,
    flag_ok: bool = False,
    inner: Any | None = None,
    target: str = "rekordbox",
) -> tuple[SafePlaylistWriter, FakeWriter, Path]:
    """Construct a SafePlaylistWriter backed by a FakeWriter + temp DB."""
    db_path = tmp_path / "fixture.db"
    db_path.write_bytes(b"SQLite fake fixture payload")
    raw = inner or FakeWriter(vendor=target)
    safe = SafePlaylistWriter(
        inner=raw,
        db_path=db_path,
        target=target,
        reason="unit-test smartlist safety",
        dry_run=dry_run,
        flag_ok=flag_ok,
    )
    return safe, raw, db_path


class TestRequireTypedConfirmPhrase:
    """Rail 3: typed confirmation prompt helper."""

    def test_accepts_exact_phrase(self) -> None:
        assert require_typed_confirm_phrase(
            input_fn=lambda _prompt: DEFAULT_CONFIRM_PHRASE,
        ) is True

    def test_rejects_wrong_phrase(self) -> None:
        assert require_typed_confirm_phrase(
            input_fn=lambda _prompt: "yes",
        ) is False

    def test_rejects_case_mismatch(self) -> None:
        assert require_typed_confirm_phrase(
            input_fn=lambda _prompt: "apply smartlist",
        ) is False

    def test_strips_trailing_whitespace(self) -> None:
        assert require_typed_confirm_phrase(
            input_fn=lambda _prompt: DEFAULT_CONFIRM_PHRASE + "\n",
        ) is True

    def test_returns_false_on_eof(self) -> None:
        def raise_eof(_prompt: str) -> str:
            raise EOFError()

        assert require_typed_confirm_phrase(input_fn=raise_eof) is False

    def test_returns_false_on_keyboard_interrupt(self) -> None:
        def raise_kbi(_prompt: str) -> str:
            raise KeyboardInterrupt()

        assert require_typed_confirm_phrase(input_fn=raise_kbi) is False

    def test_custom_phrase(self) -> None:
        assert require_typed_confirm_phrase(
            "NUCLEAR", input_fn=lambda _p: "NUCLEAR",
        ) is True


class TestSafePlaylistWriterDryRun:
    """Rail 4: dry-run default blocks ALL writes until opted out."""

    def test_default_dry_run_is_true(self, tmp_path) -> None:
        safe, _raw, _db = _make_safe_writer(tmp_path)
        assert safe.dry_run is True
        assert safe.flag_ok is False

    def test_dry_run_create_is_noop(self, tmp_path) -> None:
        safe, raw, _db = _make_safe_writer(tmp_path)
        safe.create_playlist("[SL] DR", ["sid-1", "sid-2"])
        assert raw.calls == []  # inner never called
        assert raw.playlists == {}

    def test_dry_run_apply_diff_is_noop(self, tmp_path) -> None:
        safe, raw, _db = _make_safe_writer(tmp_path)
        safe.apply_diff("[SL] DR", added=["sid-1"], removed=[])
        assert raw.calls == []

    def test_dry_run_session_yields_self_without_opening_live(
        self, tmp_path,
    ) -> None:
        """safe_writer_session in dry-run must NOT open LiveWriteSession.

        This is important: LiveWriteSession would pgrep and backup the DB,
        neither of which is safe (or possible) in a test without mocks.
        """
        safe, _raw, _db = _make_safe_writer(tmp_path)
        with safe_writer_session(safe) as s:
            assert s is safe
            # The session helper must NOT have set _session.
            assert safe._session is None

    def test_playlist_exists_passthrough_even_in_dry_run(
        self, tmp_path,
    ) -> None:
        """Read-only playlist_exists bypasses all rails."""
        inner = FakeWriter(vendor="rekordbox", playlists={"[SL] X": []})
        safe, _raw, _db = _make_safe_writer(tmp_path, inner=inner)
        assert safe.playlist_exists("[SL] X") is True
        assert safe.playlist_exists("[SL] Y") is False


class TestSafePlaylistWriterLiveGuards:
    """Rail 3 + 4 guards: live writes require flag + active session."""

    def test_live_without_flag_refuses(self, tmp_path) -> None:
        safe, raw, _db = _make_safe_writer(
            tmp_path, dry_run=False, flag_ok=False,
        )
        with pytest.raises(RuntimeError, match="typed-confirm flag not set"):
            safe.create_playlist("[SL] A", ["sid-1"])
        assert raw.calls == []

    def test_live_without_session_refuses(self, tmp_path) -> None:
        safe, raw, _db = _make_safe_writer(
            tmp_path, dry_run=False, flag_ok=True,
        )
        # Called outside safe_writer_session() -> _session is None.
        with pytest.raises(RuntimeError, match="no active LiveWriteSession"):
            safe.create_playlist("[SL] B", ["sid-1"])
        assert raw.calls == []

    def test_apply_diff_live_without_flag_refuses(self, tmp_path) -> None:
        safe, raw, _db = _make_safe_writer(
            tmp_path, dry_run=False, flag_ok=False,
        )
        with pytest.raises(RuntimeError, match="typed-confirm flag not set"):
            safe.apply_diff("[SL] A", added=["sid-1"], removed=[])
        assert raw.calls == []


class TestSafePlaylistWriterPgrepRail:
    """Rail 1: pgrep gate (via LiveWriteSession.process_gate)."""

    def test_pgrep_abort_blocks_create(
        self, tmp_path, monkeypatch,
    ) -> None:
        # Force the process gate to report "running" -> SafetyAbort.
        monkeypatch.setattr(
            "apps.sync.safety._is_running", lambda _name: True,
        )
        safe, raw, _db = _make_safe_writer(
            tmp_path, dry_run=False, flag_ok=True,
        )
        with pytest.raises(SafetyAbort, match="Rekordbox is running"):
            with safe_writer_session(safe):
                pass  # entry should raise before yielding
        assert raw.calls == []


class TestSafePlaylistWriterBackupRail:
    """Rail 2: timestamped backup before any write."""

    def test_backup_created_on_session_entry(
        self, tmp_path, monkeypatch,
    ) -> None:
        monkeypatch.setattr(
            "apps.sync.safety._is_running", lambda _name: False,
        )
        safe, _raw, db_path = _make_safe_writer(
            tmp_path, dry_run=False, flag_ok=True,
        )
        with safe_writer_session(safe, reversal_root=tmp_path / "reversal"):
            # A .bak.<ISO> sibling of db_path must exist.
            backups = list(db_path.parent.glob(f"{db_path.name}.bak.*"))
            assert len(backups) == 1
            assert backups[0].read_bytes() == db_path.read_bytes()

    def test_backup_missing_source_aborts(
        self, tmp_path, monkeypatch,
    ) -> None:
        """If the target DB doesn't exist we abort (no backup possible)."""
        monkeypatch.setattr(
            "apps.sync.safety._is_running", lambda _name: False,
        )
        safe, _raw, db_path = _make_safe_writer(
            tmp_path, dry_run=False, flag_ok=True,
        )
        db_path.unlink()
        with pytest.raises(SafetyAbort, match="DB to back up is missing"):
            with safe_writer_session(safe, reversal_root=tmp_path / "reversal"):
                pass


class TestSafePlaylistWriterVerifyRail:
    """Rail 5: post-write verify via injected verifier."""

    def test_verify_ok_writes_reverse_snippet(
        self, tmp_path, monkeypatch,
    ) -> None:
        monkeypatch.setattr(
            "apps.sync.safety._is_running", lambda _name: False,
        )
        safe, raw, _db = _make_safe_writer(
            tmp_path, dry_run=False, flag_ok=True,
        )
        verifier_calls: list[tuple[str, list[str]]] = []

        def verifier(name: str, expected: list[str]) -> bool:
            verifier_calls.append((name, list(expected)))
            return True

        reversal_root = tmp_path / "reversal"
        with safe_writer_session(
            safe, verifier=verifier, reversal_root=reversal_root,
        ) as s:
            s.create_playlist("[SL] V", ["sid-1", "sid-2"])
        # Verifier was called with the expected membership.
        assert verifier_calls == [("[SL] V", ["sid-1", "sid-2"])]
        # Reverse snippet was emitted.
        scripts = list(reversal_root.rglob("reverse.sh"))
        assert len(scripts) == 1
        body = scripts[0].read_text()
        assert "# revert smartlist create" in body
        assert "[SL] V" in body
        # Underlying writer DID fire (verify passed).
        assert raw.playlists == {"[SL] V": ["sid-1", "sid-2"]}

    def test_verify_fail_aborts_session(
        self, tmp_path, monkeypatch,
    ) -> None:
        monkeypatch.setattr(
            "apps.sync.safety._is_running", lambda _name: False,
        )
        safe, raw, _db = _make_safe_writer(
            tmp_path, dry_run=False, flag_ok=True,
        )
        # Verifier returns False -> verify_readback fails -> session
        # aborts the batch via LiveWriteSession's verify-failure path.
        # Non-tty stdin -> default action is "abort".
        def bad_verifier(_name: str, _expected: list[str]) -> bool:
            return False

        reversal_root = tmp_path / "reversal"
        with pytest.raises(SafetyAbort, match="verify_readback failed"):
            with safe_writer_session(
                safe, verifier=bad_verifier, reversal_root=reversal_root,
            ) as s:
                s.create_playlist("[SL] Bad", ["sid-1"])
        # The inner writer DID run (the verify happens AFTER the write).
        assert raw.playlists == {"[SL] Bad": ["sid-1"]}


class TestSafePlaylistWriterReversalRail:
    """Rail 6: reversal script content + path layout."""

    def test_reversal_script_header_and_shebang(
        self, tmp_path, monkeypatch,
    ) -> None:
        monkeypatch.setattr(
            "apps.sync.safety._is_running", lambda _name: False,
        )
        safe, _raw, _db = _make_safe_writer(
            tmp_path, dry_run=False, flag_ok=True,
        )
        reversal_root = tmp_path / "reversal"
        with safe_writer_session(safe, reversal_root=reversal_root):
            pass
        scripts = list(reversal_root.rglob("reverse.sh"))
        assert len(scripts) == 1
        content = scripts[0].read_text()
        assert content.startswith("#!/usr/bin/env bash")
        assert "target=rekordbox" in content
        # Last-resort restore is always appended on clean exit.
        assert "Last-resort full restore" in content

    def test_apply_diff_appends_reverse_snippet(
        self, tmp_path, monkeypatch,
    ) -> None:
        monkeypatch.setattr(
            "apps.sync.safety._is_running", lambda _name: False,
        )
        inner = FakeWriter(
            vendor="rekordbox", playlists={"[SL] R": ["sid-1", "sid-2"]},
        )
        safe, _raw, _db = _make_safe_writer(
            tmp_path, dry_run=False, flag_ok=True, inner=inner,
        )
        reversal_root = tmp_path / "reversal"
        with safe_writer_session(safe, reversal_root=reversal_root) as s:
            s.apply_diff("[SL] R", added=["sid-3"], removed=["sid-1"])
        scripts = list(reversal_root.rglob("reverse.sh"))
        body = scripts[0].read_text()
        assert "# revert smartlist diff on '[SL] R'" in body
        assert "added=['sid-3']" in body
        assert "removed=['sid-1']" in body


class TestSafePlaylistWriterVendorPassthrough:
    """The adapter is a drop-in PlaylistWriter (vendor preserved)."""

    def test_vendor_matches_inner(self, tmp_path) -> None:
        safe_rb, _r1, _d1 = _make_safe_writer(tmp_path, target="rekordbox")
        assert safe_rb.vendor == "rekordbox"

        # Make the inner writer a djay-flavoured fake; the safe adapter
        # should surface the inner.vendor regardless of `target`.
        inner = FakeWriter(vendor="djay")
        safe_djay = SafePlaylistWriter(
            inner=inner,
            db_path=tmp_path / "djay.db",
            target="djay",
        )
        assert safe_djay.vendor == "djay"

    def test_target_validation_rejects_bogus_target(self, tmp_path) -> None:
        """Only rekordbox|djay are accepted when opening a live session."""
        (tmp_path / "f.db").write_bytes(b"x")
        safe = SafePlaylistWriter(
            inner=FakeWriter(),
            db_path=tmp_path / "f.db",
            target="bogus",
            dry_run=False,
            flag_ok=True,
        )
        with pytest.raises(ValueError, match="target must be"):
            with safe_writer_session(safe):
                pass

