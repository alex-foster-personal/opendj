"""LIBM-21: DELETE /playlists/{id}/items/{item_id} without rewriting membership.

[if] a playlist item is removed [then] only its row is tombstoned, neighbors stay put, [else stop].
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.smartlists.repo import SmartlistsRepo
from apps.webui.server.app import create_app
from apps.webui.server.routes import playlist_write
from apps.webui.server.sqlite_backend import SqliteBackend

TRACK_IDS: list[str] = ["t-001", "t-002", "t-003", "t-004"]

_BPM_RULE: dict = {
    "op": "and",
    "children": [
        {"field": "bpm", "op": ">=", "value": 120},
        {"field": "bpm", "op": "<=", "value": 130},
    ],
}

pytestmark = pytest.mark.requirement("LIBM-21")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="test-seed")
    try:
        for i, sid in enumerate(TRACK_IDS, start=1):
            writer.upsert_track(
                stable_id=sid,
                stable_id_tier="inferred",
                title=f"Track {i}",
                artists=[f"Artist {i}"],
                album=None,
                isrc=None,
                duration_ms=180_000 + i,
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


def _create(client: TestClient, name: str = "My Set") -> tuple[dict, str]:
    r = client.post("/api/v1/playlists", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json(), r.headers["ETag"]


def _put_tracks(client: TestClient, pid: str, etag: str, ids: list[str]) -> str:
    r = client.put(
        f"/api/v1/playlists/{pid}/tracks",
        json={"stable_ids": ids},
        headers={"If-Match": etag},
    )
    assert r.status_code == 200, r.text
    return r.headers["ETag"]


def dump_members(db_path: Path, playlist_id: str) -> dict[str, tuple]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT item_id, stable_id, position, order_key, updated_at, "
            "origin_device_id, deleted_at FROM playlist_memberships "
            "WHERE playlist_id=? ORDER BY item_id",
            (playlist_id,),
        ).fetchall()
        return {row[0]: row for row in rows}
    finally:
        conn.close()


def _event_kinds(db_path: Path) -> list[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return [row[0] for row in conn.execute("SELECT kind FROM events ORDER BY id")]
    finally:
        conn.close()


def _live_item_ids(client: TestClient, pid: str) -> list[str]:
    detail = client.get(f"/api/v1/playlists/{pid}")
    assert detail.status_code == 200, detail.text
    return [t["item_id"] for t in detail.json()["tracks"]]


def test_remove_one_member_neighbors_untouched(
    client: TestClient,
    db_path: Path,
) -> None:
    body, etag = _create(client)
    pid = body["playlist_id"]
    etag = _put_tracks(client, pid, etag, ["t-001", "t-002", "t-003"])
    before = dump_members(db_path, pid)
    item_ids = _live_item_ids(client, pid)
    middle_id = item_ids[1]
    old_etag = etag
    r = client.delete(f"/api/v1/playlists/{pid}/items/{middle_id}")
    assert r.status_code == 200, r.text
    after = dump_members(db_path, pid)
    assert set(after.keys()) == set(before.keys())
    for iid, row in before.items():
        if iid == middle_id:
            assert row[6] is None
            assert after[iid][6] is not None
            assert after[iid][:4] == row[:4]
        else:
            assert after[iid] == row
    assert r.json()["items"] == ["t-001", "t-003"]
    assert "playlist.memberships.remove" in _event_kinds(db_path)
    assert r.headers["ETag"] != old_etag
    get_r = client.get(f"/api/v1/playlists/{pid}")
    assert get_r.json()["items"] == r.json()["items"]


def test_missing_item_id_404(client: TestClient, db_path: Path) -> None:
    body, etag = _create(client)
    pid = body["playlist_id"]
    etag = _put_tracks(client, pid, etag, ["t-001", "t-002"])
    before = dump_members(db_path, pid)
    kinds_before = _event_kinds(db_path)
    fake_id = uuid.uuid4().hex
    r = client.delete(f"/api/v1/playlists/{pid}/items/{fake_id}")
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"
    assert pid in r.json()["message"]
    assert fake_id in r.json()["message"]
    assert dump_members(db_path, pid) == before
    assert _event_kinds(db_path) == kinds_before

    body2, etag2 = _create(client, "Other")
    pid2 = body2["playlist_id"]
    etag2 = _put_tracks(client, pid2, etag2, ["t-003"])
    other_id = _live_item_ids(client, pid2)[0]
    before2 = dump_members(db_path, pid2)
    r2 = client.delete(f"/api/v1/playlists/{pid}/items/{other_id}")
    assert r2.status_code == 404
    assert pid in r2.json()["message"]
    assert other_id in r2.json()["message"]
    assert dump_members(db_path, pid) == before
    assert dump_members(db_path, pid2) == before2


def test_idempotent_remove_still_404(client: TestClient, db_path: Path) -> None:
    body, etag = _create(client)
    pid = body["playlist_id"]
    etag = _put_tracks(client, pid, etag, ["t-001", "t-002", "t-003"])
    item_id = _live_item_ids(client, pid)[0]
    r1 = client.delete(f"/api/v1/playlists/{pid}/items/{item_id}")
    assert r1.status_code == 200, r1.text
    after_first = dump_members(db_path, pid)
    r2 = client.delete(f"/api/v1/playlists/{pid}/items/{item_id}")
    assert r2.status_code == 404
    assert dump_members(db_path, pid) == after_first


def test_smartlist_refused(client: TestClient, db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        sid = SmartlistsRepo(conn).create("120s", _BPM_RULE).id
        conn.commit()
    finally:
        conn.close()
    r = client.delete(f"/api/v1/playlists/{sid}/items/deadbeef")
    assert r.status_code == 422
    assert r.json()["error"] == "smartlist_immutable"
    assert "smartlist" in r.json()["message"].lower()
    r404 = client.delete("/api/v1/playlists/no-such-playlist/items/deadbeef")
    assert r404.status_code == 404
    assert r404.json()["error"] == "not_found"


def test_undo_restores_same_item_id_and_order_key(
    client: TestClient,
    db_path: Path,
) -> None:
    body, etag = _create(client)
    pid = body["playlist_id"]
    etag = _put_tracks(client, pid, etag, ["t-001", "t-002", "t-003"])
    before = dump_members(db_path, pid)
    item_id = _live_item_ids(client, pid)[1]
    pre_tuple = before[item_id]
    r = client.delete(f"/api/v1/playlists/{pid}/items/{item_id}")
    assert r.status_code == 200, r.text
    undo = client.post("/api/v1/playlist-history/undo")
    assert undo.status_code == 200, undo.text
    after = dump_members(db_path, pid)
    live = after[item_id]
    assert live[6] is None
    assert live[:4] == pre_tuple[:4]
    get_r = client.get(f"/api/v1/playlists/{pid}")
    assert get_r.json()["items"] == ["t-001", "t-002", "t-003"]
    for iid, row in before.items():
        if iid != item_id:
            assert after[iid] == row
    redo = client.post("/api/v1/playlist-history/redo")
    assert redo.status_code == 200, redo.text
    after_redo = dump_members(db_path, pid)
    assert after_redo[item_id][6] is not None
    for iid, row in after.items():
        if iid != item_id:
            assert after_redo[iid] == row


def test_duplicate_stable_ids(client: TestClient, db_path: Path) -> None:
    body, etag = _create(client)
    pid = body["playlist_id"]
    etag = _put_tracks(client, pid, etag, ["t-001", "t-002", "t-001"])
    before = dump_members(db_path, pid)
    item_ids = _live_item_ids(client, pid)
    first_a = item_ids[0]
    r = client.delete(f"/api/v1/playlists/{pid}/items/{first_a}")
    assert r.status_code == 200, r.text
    assert r.json()["items"] == ["t-002", "t-001"]
    surviving = next(row for row in before.values() if row[1] == "t-001" and row[0] != first_a)
    after = dump_members(db_path, pid)
    assert after[surviving[0]][:4] == surviving[:4]
    client.post("/api/v1/playlist-history/undo")
    restored = dump_members(db_path, pid)
    assert restored[first_a][:4] == before[first_a][:4]


def test_no_if_match_required(client: TestClient) -> None:
    body, etag = _create(client)
    pid = body["playlist_id"]
    etag = _put_tracks(client, pid, etag, ["t-001"])
    item_id = _live_item_ids(client, pid)[0]
    r = client.delete(f"/api/v1/playlists/{pid}/items/{item_id}")
    assert r.status_code == 200, r.text


def test_openapi_documents_delete(client: TestClient) -> None:
    spec = client.get("/openapi.json")
    assert spec.status_code == 200
    paths = spec.json()["paths"]
    assert "/api/v1/playlists/{playlist_id}/items/{item_id}" in paths
    assert "delete" in paths["/api/v1/playlists/{playlist_id}/items/{item_id}"]


def test_openapi_documents_remove_post(client: TestClient) -> None:
    spec = client.get("/openapi.json")
    assert spec.status_code == 200
    paths = spec.json()["paths"]
    assert "/api/v1/playlists/{playlist_id}/items:remove" in paths
    assert "post" in paths["/api/v1/playlists/{playlist_id}/items:remove"]


def test_get_detail_carries_item_id(client: TestClient, db_path: Path) -> None:
    body, etag = _create(client)
    pid = body["playlist_id"]
    etag = _put_tracks(client, pid, etag, ["t-001", "t-002", "t-003"])
    live_rows = [row for row in dump_members(db_path, pid).values() if row[6] is None]
    live_rows.sort(key=lambda r: r[3])
    detail = client.get(f"/api/v1/playlists/{pid}")
    wire_ids = [t["item_id"] for t in detail.json()["tracks"]]
    expected = [r[0] for r in live_rows]
    assert wire_ids == expected
    assert len(set(wire_ids)) == len(wire_ids)
    assert all(iid for iid in wire_ids)
