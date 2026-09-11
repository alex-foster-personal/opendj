"""Playlist write persistence: mutations survive store close/reopen.

Regression one-liners:
  * if create does not survive a closed/reopened PlaylistStore then broken
  * if duplicate does not persist membership order after reopen then broken
  * if delete does not survive reopen (row still readable) then broken
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server.app import create_app
from apps.webui.server.backend import NotFoundError
from apps.webui.server.playlist_store import PlaylistStore
from apps.webui.server.routes import playlist_write
from apps.webui.server.sqlite_backend import SqliteBackend

TRACK_IDS: list[str] = ["t-001", "t-002", "t-003", "t-004"]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Fresh tmp state.db seeded with 4 tracks via the shared-state writer."""
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="test-seed")
    try:
        for i, sid in enumerate(TRACK_IDS, start=1):
            writer.upsert_track(
                stable_id=sid, stable_id_tier="inferred",
                title=f"Track {i}", artists=[f"Artist {i}"], album=None,
                isrc=None, duration_ms=180_000 + i, file_path=None,
            )
    finally:
        writer.close()
        conn.close()
    return path


def _event_kinds(db_path: Path) -> list[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return [
            row[0] for row in conn.execute(
                "SELECT kind FROM events ORDER BY id"
            )
        ]
    finally:
        conn.close()


def test_create_survives_store_reopen(db_path: Path) -> None:
    kinds_before = _event_kinds(db_path)
    store = PlaylistStore(db_path)
    try:
        row = store.create_playlist("Keep")
        playlist_id = row.playlist_id
    finally:
        store.close()

    kinds_after = _event_kinds(db_path)
    assert "playlist.insert" in kinds_after[len(kinds_before):]

    store2 = PlaylistStore(db_path)
    try:
        reopened = store2.get_playlist_row(playlist_id)
        assert reopened.name == "Keep"
        assert reopened.items == []
    finally:
        store2.close()


def test_duplicate_survives_reopen_preserves_order(db_path: Path) -> None:
    store = PlaylistStore(db_path)
    try:
        source = store.create_playlist("Peak Hour")
        source_id = source.playlist_id
        store.replace_memberships(
            source_id, ["t-002", "t-001"], expected_etag=source.etag,
        )
        copy = store.duplicate_playlist(source_id)
        copy_id = copy.playlist_id
    finally:
        store.close()

    store2 = PlaylistStore(db_path)
    try:
        source_reopened = store2.get_playlist_row(source_id)
        copy_reopened = store2.get_playlist_row(copy_id)
        assert source_reopened.name == "Peak Hour"
        assert copy_reopened.name == "Peak Hour (copy)"
        assert copy_reopened.items == ["t-002", "t-001"]
        assert copy_id != source_id
    finally:
        store2.close()

    kinds = _event_kinds(db_path)
    assert "playlist.insert" in kinds
    assert "playlist.memberships.set" in kinds


def test_delete_survives_reopen(db_path: Path) -> None:
    store = PlaylistStore(db_path)
    try:
        row = store.create_playlist("Gone")
        playlist_id = row.playlist_id
        etag = row.etag
        store.delete_playlist(playlist_id, expected_etag=etag)
    finally:
        store.close()

    store2 = PlaylistStore(db_path)
    try:
        with pytest.raises(NotFoundError):
            store2.get_playlist_row(playlist_id)
    finally:
        store2.close()

    assert "playlist.delete" in _event_kinds(db_path)

    app = create_app(
        backend=SqliteBackend(db_path), state_db_path=str(db_path),
        bind_host="127.0.0.1", hostname="test-host",
        lock_status_fn=lambda: None, mount_frontend=False,
    )
    with TestClient(app) as client:
        assert client.get(f"/api/v1/playlists/{playlist_id}").status_code == 404
    playlist_write.close_store(app)
