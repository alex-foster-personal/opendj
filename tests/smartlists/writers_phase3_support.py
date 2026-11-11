"""Shared helpers for Phase 3 playlist-writer tests.

State and djay fixtures, rekordbox fakes, and the SafePlaylistWriter
constructor used by more than one sibling test module. The six-rail
adapter import lives here so a missing symbol skips every sibling the
same way the original module did.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

try:
    from apps.smartlists.writers import (
        FakeWriter,
        SafePlaylistWriter,
    )
except ImportError:  # pragma: no cover - WIP six-rail adapter, not yet merged
    pytest.skip(
        "apps.smartlists.writers does not yet export the six-rail adapter "
        "symbols; skipping until the Phase 3/8 SafePlaylistWriter work is "
        "committed. Tracked via CI watcher.",
        allow_module_level=True,
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
        "INSERT INTO track_vendor_ids(stable_id, vendor, vendor_id) VALUES (?, ?, ?)",
        rows,
    )
    yield conn
    conn.close()


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
        "INSERT INTO database2(collection, key, data) VALUES ('mediaItemUserData', ?, ?)",
        user_rows,
    )
    con.close()
    return db_path


def _seed_existing_playlist(djay_db: Path, name: str, member_uuids: list[str]) -> str:
    """Seed a playlist row + page(s). Returns the playlist uuid."""
    import uuid as _uuid

    from apps.sync import playlist_tsaf as ptsaf
    from apps.sync.playlist_apply import resolve_userdata_rowids

    uuid = _uuid.uuid4().hex
    blob = ptsaf.build_playlist_blob(uuid, name, kind="leaf")
    con = sqlite3.connect(str(djay_db), isolation_level=None)
    try:
        con.execute(
            "INSERT INTO database2(collection, key, data) VALUES ('mediaItemPlaylists', ?, ?)",
            (uuid, blob),
        )
        rowids = resolve_userdata_rowids(con, member_uuids)
        page_key = ptsaf.new_page_key()
        page_data = ptsaf.build_page_data(rowids)
        con.execute(
            "INSERT INTO view_mediaItemPlaylistView_page "
            '(pageKey, "group", prevPageKey, count, data) '
            "VALUES (?, ?, NULL, ?, ?)",
            (page_key, uuid, len(rowids), page_data),
        )
    finally:
        con.close()
    return uuid


# -------------------------------------------------------- SafePlaylistWriter


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
