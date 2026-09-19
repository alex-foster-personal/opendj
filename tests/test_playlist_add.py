"""LIBM-20: POST /playlists/{id}/items:add without rewriting membership.

[if] a track is added to a playlist [then] existing membership rows stay unrewritten, [else stop].
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

pytestmark = pytest.mark.requirement("LIBM-20")


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


def dump_members(db_path: Path, playlist_id: str) -> list[tuple]:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT item_id, stable_id, position, order_key, updated_at, "
            "origin_device_id, deleted_at FROM playlist_memberships "
            "WHERE playlist_id=? ORDER BY item_id",
            (playlist_id,),
        ).fetchall()
    finally:
        conn.close()


def _event_kinds(db_path: Path) -> list[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return [row[0] for row in conn.execute("SELECT kind FROM events ORDER BY id")]
    finally:
        conn.close()


def test_append_one_track_o1_write(client: TestClient, db_path: Path) -> None:
    body, etag = _create(client)
    pid = body["playlist_id"]
    etag = _put_tracks(client, pid, etag, ["t-001", "t-002", "t-003"])
    before = dump_members(db_path, pid)
    old_etag = etag
    r = client.post(
        f"/api/v1/playlists/{pid}/items:add",
        json={"stable_ids": ["t-004"]},
    )
    assert r.status_code == 200, r.text
    after = dump_members(db_path, pid)
    assert len(after) == len(before) + 1
    before_set = set(before)
    assert before_set <= set(after)
    for row in before:
        assert row in after
    new_rows = [row for row in after if row not in before_set]
    assert len(new_rows) == 1
    assert new_rows[0][1] == "t-004"
    assert r.json()["items"] == ["t-001", "t-002", "t-003", "t-004"]
    assert "playlist.memberships.add" in _event_kinds(db_path)
    assert r.headers["ETag"] != old_etag


def test_positioned_insert_neighbors_untouched(client: TestClient, db_path: Path) -> None:
    body, etag = _create(client)
    pid = body["playlist_id"]
    etag = _put_tracks(client, pid, etag, ["t-001", "t-002", "t-003"])
    before = dump_members(db_path, pid)
    r = client.post(
        f"/api/v1/playlists/{pid}/items:add",
        json={"stable_ids": ["t-004"], "position": 1},
    )
    assert r.status_code == 200, r.text
    after = dump_members(db_path, pid)
    before_by_stable = {row[1]: row for row in before}
    after_by_stable = {row[1]: row for row in after}
    for sid in ["t-001", "t-002", "t-003"]:
        assert before_by_stable[sid] == after_by_stable[sid]
    new_row = after_by_stable["t-004"]
    assert before_by_stable["t-001"][3] < new_row[3] < before_by_stable["t-002"][3]
    assert r.json()["items"] == ["t-001", "t-004", "t-002", "t-003"]


def test_omitted_position_appends_after_last(client: TestClient, db_path: Path) -> None:
    body, etag = _create(client)
    pid = body["playlist_id"]
    etag = _put_tracks(client, pid, etag, ["t-001", "t-002"])
    before = dump_members(db_path, pid)
    last_key = max(row[3] for row in before)
    r = client.post(
        f"/api/v1/playlists/{pid}/items:add",
        json={"stable_ids": ["t-003"]},
    )
    assert r.status_code == 200, r.text
    after = dump_members(db_path, pid)
    new_row = next(row for row in after if row[1] == "t-003")
    assert new_row[3] > last_key


@pytest.mark.requirement("LIBM-03")
def test_already_member_creates_second_row(client: TestClient, db_path: Path) -> None:
    body, etag = _create(client)
    pid = body["playlist_id"]
    etag = _put_tracks(client, pid, etag, ["t-001", "t-002"])
    before = dump_members(db_path, pid)
    original_t001 = next(row for row in before if row[1] == "t-001")
    r = client.post(
        f"/api/v1/playlists/{pid}/items:add",
        json={"stable_ids": ["t-001"]},
    )
    assert r.status_code == 200, r.text
    after = dump_members(db_path, pid)
    assert len(after) == len(before) + 1
    assert original_t001 in after
    t001_rows = [row for row in after if row[1] == "t-001"]
    assert len(t001_rows) == 2
    assert t001_rows[0][0] != t001_rows[1][0]
    assert r.json()["items"].count("t-001") == 2


def test_smartlist_refused(client: TestClient, db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        sid = SmartlistsRepo(conn).create("120s", _BPM_RULE).id
        conn.commit()
    finally:
        conn.close()
    r = client.post(
        f"/api/v1/playlists/{sid}/items:add",
        json={"stable_ids": ["t-001"]},
    )
    assert r.status_code == 422
    assert r.json()["error"] == "smartlist_immutable"
    assert "smartlist" in r.json()["message"].lower()
    r404 = client.post(
        "/api/v1/playlists/no-such-playlist/items:add",
        json={"stable_ids": ["t-001"]},
    )
    assert r404.status_code == 404
    assert r404.json()["error"] == "not_found"


def test_unknown_track_422(client: TestClient, db_path: Path) -> None:
    body, etag = _create(client)
    pid = body["playlist_id"]
    etag = _put_tracks(client, pid, etag, ["t-001"])
    before = dump_members(db_path, pid)
    r = client.post(
        f"/api/v1/playlists/{pid}/items:add",
        json={"stable_ids": ["t-missing"]},
    )
    assert r.status_code == 422
    assert dump_members(db_path, pid) == before


def test_position_out_of_range_422(client: TestClient, db_path: Path) -> None:
    body, etag = _create(client)
    pid = body["playlist_id"]
    etag = _put_tracks(client, pid, etag, ["t-001", "t-002"])
    before = dump_members(db_path, pid)
    r = client.post(
        f"/api/v1/playlists/{pid}/items:add",
        json={"stable_ids": ["t-003"], "position": 99},
    )
    assert r.status_code == 422
    assert dump_members(db_path, pid) == before


def test_no_if_match_required(client: TestClient) -> None:
    body, _ = _create(client)
    r = client.post(
        f"/api/v1/playlists/{body['playlist_id']}/items:add",
        json={"stable_ids": ["t-001"]},
    )
    assert r.status_code == 200


def test_openapi_documents_add_endpoint(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    assert "/api/v1/playlists/{playlist_id}/items:add" in schema["paths"]
    assert "post" in schema["paths"]["/api/v1/playlists/{playlist_id}/items:add"]


def test_bulk_all_new(client: TestClient, db_path: Path) -> None:
    body, etag = _create(client, "Bulk")
    pid = body["playlist_id"]
    etag = _put_tracks(client, pid, etag, ["t-001"])
    before = dump_members(db_path, pid)
    r = client.post(
        f"/api/v1/playlists/{pid}/items:add",
        json={"stable_ids": ["t-002", "t-003"]},
    )
    assert r.status_code == 200, r.text
    after = dump_members(db_path, pid)
    assert len(after) == len(before) + 2
    for row in before:
        assert row in after
    assert r.json()["items"] == ["t-001", "t-002", "t-003"]


@pytest.mark.requirement("LIBM-03")
def test_bulk_with_one_already_present_adds_both(client: TestClient, db_path: Path) -> None:
    body, etag = _create(client)
    pid = body["playlist_id"]
    etag = _put_tracks(client, pid, etag, ["t-001", "t-002"])
    before = dump_members(db_path, pid)
    r = client.post(
        f"/api/v1/playlists/{pid}/items:add",
        json={"stable_ids": ["t-001", "t-003"]},
    )
    assert r.status_code == 200, r.text
    after = dump_members(db_path, pid)
    assert len(after) == len(before) + 2
    for row in before:
        assert row in after
    assert r.json()["items"] == ["t-001", "t-002", "t-001", "t-003"]
