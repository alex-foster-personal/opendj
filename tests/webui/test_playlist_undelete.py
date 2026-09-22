"""LIBMX-03 HTTP playlist delete/undelete regression tests.

[if] a playlist is deleted then POST :undelete [then] it reappears in the live list with original memberships intact, [else stop].
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server.app import create_app
from apps.webui.server.routes import playlist_write
from apps.webui.server.sqlite_backend import SqliteBackend

TRACK_A = "a" * 40
TRACK_B = "b" * 40

pytestmark = pytest.mark.requirement("LIBMX-03")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="test-seed")
    try:
        for sid, title in ((TRACK_A, "Track A"), (TRACK_B, "Track B")):
            writer.upsert_track(
                stable_id=sid,
                stable_id_tier="inferred",
                title=title,
                artists=["Artist"],
                album=None,
                isrc=None,
                duration_ms=180_000,
                file_path=None,
            )
    finally:
        writer.close()
        conn.close()
    return path


@pytest.fixture
def client(db_path: Path) -> Iterator[TestClient]:
    app = create_app(
        backend=SqliteBackend(db_path),
        state_db_path=str(db_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
        mount_frontend=False,
    )
    with TestClient(app) as c:
        yield c
    playlist_write.close_store(app)


def _create_playlist(client: TestClient, name: str) -> tuple[dict, str]:
    r = client.post("/api/v1/playlists", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json(), r.headers["ETag"]


def _put_tracks(
    client: TestClient,
    playlist_id: str,
    stable_ids: list[str],
    etag: str,
) -> str:
    r = client.put(
        f"/api/v1/playlists/{playlist_id}/tracks",
        json={"stable_ids": stable_ids},
        headers={"If-Match": etag},
    )
    assert r.status_code == 200, r.text
    return r.headers["ETag"]


def test_deleted_playlist_lists_and_live_get_404s(
    client: TestClient,
) -> None:
    """[if] a playlist with members is deleted [then] GET /deleted lists it and
    live GET 404s, [else stop]."""
    created, etag = _create_playlist(client, "Trash me")
    playlist_id = created["playlist_id"]
    etag = _put_tracks(client, playlist_id, [TRACK_A, TRACK_B], etag)

    delete = client.delete(
        f"/api/v1/playlists/{playlist_id}",
        headers={"If-Match": etag},
    )
    assert delete.status_code == 204, delete.text

    live_list = client.get("/api/v1/playlists")
    assert all(row["playlist_id"] != playlist_id for row in live_list.json())

    deleted = client.get("/api/v1/playlists/deleted")
    assert deleted.status_code == 200, deleted.text
    rows = deleted.json()
    match = next(row for row in rows if row["playlist_id"] == playlist_id)
    assert match["name"] == "Trash me"
    assert match["track_count"] == 2
    assert match["deleted_at"]

    live_get = client.get(f"/api/v1/playlists/{playlist_id}")
    assert live_get.status_code == 404


def test_undelete_restores_memberships_intact(
    client: TestClient,
    db_path: Path,
) -> None:
    """[if] a deleted playlist is undeleted [then] memberships match the
    pre-delete snapshot, [else stop]."""
    created, etag = _create_playlist(client, "Restore me")
    playlist_id = created["playlist_id"]
    etag = _put_tracks(client, playlist_id, [TRACK_A, TRACK_B], etag)

    conn = sqlite3.connect(str(db_path))
    try:
        before = conn.execute(
            "SELECT item_id, stable_id, position, order_key "
            "FROM playlist_memberships WHERE playlist_id = ? "
            "AND deleted_at IS NULL ORDER BY order_key",
            (playlist_id,),
        ).fetchall()
        before_items = [row[1] for row in before]
    finally:
        conn.close()

    detail = client.get(f"/api/v1/playlists/{playlist_id}")
    assert detail.status_code == 200
    assert detail.json()["items"] == before_items

    delete = client.delete(
        f"/api/v1/playlists/{playlist_id}",
        headers={"If-Match": detail.headers["ETag"]},
    )
    assert delete.status_code == 204

    restore = client.post(f"/api/v1/playlists/{playlist_id}:undelete")
    assert restore.status_code == 200, restore.text
    assert "ETag" in restore.headers
    assert restore.json()["items"] == before_items

    conn = sqlite3.connect(str(db_path))
    try:
        after = conn.execute(
            "SELECT item_id, stable_id, position, order_key "
            "FROM playlist_memberships WHERE playlist_id = ? "
            "AND deleted_at IS NULL ORDER BY order_key",
            (playlist_id,),
        ).fetchall()
    finally:
        conn.close()
    assert after == before

    live_list = client.get("/api/v1/playlists")
    assert any(row["playlist_id"] == playlist_id for row in live_list.json())
    assert client.get("/api/v1/playlists/deleted").json() == []
    detail_after = client.get(f"/api/v1/playlists/{playlist_id}")
    assert detail_after.status_code == 200
    assert detail_after.json()["items"] == before_items


def test_undelete_live_409_and_missing_404(client: TestClient) -> None:
    """[if] undelete targets a live or missing playlist [then] 409 or 404 is
    returned, [else stop]."""
    created, _etag = _create_playlist(client, "Still live")
    playlist_id = created["playlist_id"]

    live = client.post(f"/api/v1/playlists/{playlist_id}:undelete")
    assert live.status_code == 409
    assert live.json()["detail"]["error"] == "not_deleted"

    missing = client.post("/api/v1/playlists/pl-does-not-exist:undelete")
    assert missing.status_code == 404


def test_openapi_includes_deleted_and_undelete_paths(
    db_path: Path,
) -> None:
    """[if] the app OpenAPI schema is generated [then] deleted and undelete paths
    are present, [else stop]."""
    app = create_app(
        backend=SqliteBackend(db_path),
        state_db_path=str(db_path),
        mount_frontend=False,
    )
    paths = app.openapi()["paths"]
    assert "/api/v1/playlists/deleted" in paths
    assert "/api/v1/playlists/{playlist_id}:undelete" in paths
