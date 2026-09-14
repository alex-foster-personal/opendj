"""LIBM-22: POST /playlists/{id}/items:move without rewriting neighbors.

[if] a slice of playlist items moves [then] only the moved rows receive new order keys, [else stop].
"""

from __future__ import annotations

import json
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

TRACK_IDS: list[str] = ["t-001", "t-002", "t-003", "t-004", "t-005", "t-006"]

_BPM_RULE: dict = {
    "op": "and",
    "children": [
        {"field": "bpm", "op": ">=", "value": 120},
        {"field": "bpm", "op": "<=", "value": 130},
    ],
}

pytestmark = pytest.mark.requirement("LIBM-22")


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


def _move(
    client: TestClient,
    pid: str,
    etag: str,
    body: dict,
):
    return client.post(
        f"/api/v1/playlists/{pid}/items:move",
        json=body,
        headers={"If-Match": etag},
    )


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


def _live_order_keys(db_path: Path, playlist_id: str) -> list[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT order_key FROM playlist_memberships "
            "WHERE playlist_id=? AND deleted_at IS NULL "
            "ORDER BY COALESCE(order_key, printf('%08d', position)), position",
            (playlist_id,),
        ).fetchall()
        return [row[0] for row in rows]
    finally:
        conn.close()


def test_move_slice_neighbors_untouched(
    client: TestClient,
    db_path: Path,
) -> None:
    body, etag = _create(client)
    pid = body["playlist_id"]
    stable = ["t-001", "t-002", "t-003", "t-004", "t-005", "t-006"]
    etag = _put_tracks(client, pid, etag, stable)
    before = dump_members(db_path, pid)
    ids = _live_item_ids(client, pid)
    old_etag = etag
    r = _move(
        client,
        pid,
        etag,
        {
            "range_start": ids[1],
            "range_length": 2,
            "range_end": ids[2],
            "after_item_id": ids[-1],
        },
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["renumbered"] is False
    after = dump_members(db_path, pid)
    assert set(after.keys()) == set(before.keys())
    moved = {ids[1], ids[2]}
    for iid, row in before.items():
        if iid in moved:
            assert after[iid][6] is None
            assert after[iid][:3] == row[:3]
            assert after[iid][3] != row[3]
        else:
            assert after[iid] == row
    assert payload["items"] == ["t-001", "t-004", "t-005", "t-006", "t-002", "t-003"]
    assert "playlist.memberships.move" in _event_kinds(db_path)
    assert r.headers["ETag"] != old_etag
    keys = _live_order_keys(db_path, pid)
    assert len(keys) == len(set(keys))
    assert keys == sorted(keys)


def test_non_contiguous_slice_refused(
    client: TestClient,
    db_path: Path,
) -> None:
    body, etag = _create(client)
    pid = body["playlist_id"]
    etag = _put_tracks(client, pid, etag, ["t-001", "t-002", "t-003", "t-004"])
    before = dump_members(db_path, pid)
    ids = _live_item_ids(client, pid)
    kinds_before = _event_kinds(db_path)
    r = _move(
        client,
        pid,
        etag,
        {
            "range_start": ids[0],
            "range_length": 2,
            "range_end": ids[2],
            "after_item_id": ids[3],
        },
    )
    assert r.status_code == 422
    err = r.json()
    assert err["error"] == "slice_not_contiguous"
    msg = err["message"].lower()
    assert "not contiguous" in msg or "gap" in msg
    assert ids[2] in err["message"]
    assert ids[1] in err["message"]
    assert dump_members(db_path, pid) == before
    assert _event_kinds(db_path) == kinds_before


def test_target_inside_slice_refused(
    client: TestClient,
    db_path: Path,
) -> None:
    body, etag = _create(client)
    pid = body["playlist_id"]
    etag = _put_tracks(client, pid, etag, ["t-001", "t-002", "t-003", "t-004", "t-005"])
    before = dump_members(db_path, pid)
    ids = _live_item_ids(client, pid)
    items_before = client.get(f"/api/v1/playlists/{pid}").json()["items"]
    r = _move(
        client,
        pid,
        etag,
        {
            "range_start": ids[1],
            "range_length": 3,
            "range_end": ids[3],
            "before_item_id": ids[2],
        },
    )
    assert r.status_code == 422
    assert r.json()["error"] == "target_inside_slice"
    assert dump_members(db_path, pid) == before
    assert client.get(f"/api/v1/playlists/{pid}").json()["items"] == items_before


def test_precision_exhaustion_renumbers(
    client: TestClient,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import apps.shared.state.order_key as order_key_mod

    monkeypatch.setattr(order_key_mod, "MAX_ORDER_KEY_LEN", 8)
    body, etag = _create(client)
    pid = body["playlist_id"]
    stable = ["t-001", "t-002", "t-003", "t-004", "t-005"]
    etag = _put_tracks(client, pid, etag, stable)
    ids = _live_item_ids(client, pid)
    r = _move(
        client,
        pid,
        etag,
        {
            "range_start": ids[-1],
            "range_length": 1,
            "range_end": ids[-1],
            "before_item_id": ids[1],
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["renumbered"] is True
    events = [
        json.loads(row[1])
        for row in sqlite3.connect(str(db_path))
        .execute("SELECT kind, payload_json FROM events WHERE kind='playlist.memberships.move'")
        .fetchall()
    ]
    assert events[-1]["renumbered"] is True
    after = dump_members(db_path, pid)
    for row in after.values():
        if row[6] is None:
            assert row[3] == f"{int(row[3]):08d}" or row[3].isdigit()
    keys = _live_order_keys(db_path, pid)
    assert len(keys) == len(set(keys))
    assert r.json()["items"] == ["t-001", "t-005", "t-002", "t-003", "t-004"]


def test_five_row_slice_one_call(
    client: TestClient,
    db_path: Path,
) -> None:
    body, etag = _create(client)
    pid = body["playlist_id"]
    stable = ["t-001", "t-002", "t-003", "t-004", "t-005", "t-006"]
    etag = _put_tracks(client, pid, etag, stable)
    before = dump_members(db_path, pid)
    ids = _live_item_ids(client, pid)
    r = _move(
        client,
        pid,
        etag,
        {
            "range_start": ids[0],
            "range_length": 5,
            "range_end": ids[4],
            "after_item_id": ids[5],
        },
    )
    assert r.status_code == 200, r.text
    moved = set(ids[:5])
    after = dump_members(db_path, pid)
    for iid, row in before.items():
        if iid not in moved:
            assert after[iid] == row


def test_if_match_missing_428(client: TestClient, db_path: Path) -> None:
    body, etag = _create(client)
    pid = body["playlist_id"]
    etag = _put_tracks(client, pid, etag, ["t-001", "t-002", "t-003"])
    before = dump_members(db_path, pid)
    ids = _live_item_ids(client, pid)
    r = client.post(
        f"/api/v1/playlists/{pid}/items:move",
        json={
            "range_start": ids[0],
            "range_length": 1,
            "range_end": ids[0],
            "after_item_id": ids[2],
        },
    )
    assert r.status_code == 428
    assert dump_members(db_path, pid) == before


def test_stale_if_match_409(client: TestClient, db_path: Path) -> None:
    body, etag = _create(client)
    pid = body["playlist_id"]
    etag = _put_tracks(client, pid, etag, ["t-001", "t-002", "t-003", "t-004"])
    before = dump_members(db_path, pid)
    ids = _live_item_ids(client, pid)
    r = _move(
        client,
        pid,
        '"stale-etag"',
        {
            "range_start": ids[0],
            "range_length": 1,
            "range_end": ids[0],
            "after_item_id": ids[3],
        },
    )
    assert r.status_code == 409
    assert r.json()["error"] == "conflict"
    assert dump_members(db_path, pid) == before


def test_smartlist_refused(client: TestClient, db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        sid = SmartlistsRepo(conn).create("120s", _BPM_RULE).id
        conn.commit()
    finally:
        conn.close()
    r = _move(
        client,
        sid,
        '"x"',
        {
            "range_start": "a",
            "range_length": 1,
            "after_item_id": "b",
        },
    )
    assert r.status_code == 422
    assert r.json()["error"] == "smartlist_immutable"
    r404 = client.post(
        "/api/v1/playlists/no-such-playlist/items:move",
        json={"range_start": "a", "range_length": 1, "after_item_id": "b"},
        headers={"If-Match": '"x"'},
    )
    assert r404.status_code == 404
    assert r404.json()["error"] == "not_found"


def test_openapi_documents_move(client: TestClient) -> None:
    spec = client.get("/openapi.json")
    assert spec.status_code == 200
    paths = spec.json()["paths"]
    assert "/api/v1/playlists/{playlist_id}/items:move" in paths
    assert "post" in paths["/api/v1/playlists/{playlist_id}/items:move"]


def test_true_no_op_same_position(
    client: TestClient,
    db_path: Path,
) -> None:
    body, etag = _create(client)
    pid = body["playlist_id"]
    etag = _put_tracks(client, pid, etag, ["t-001", "t-002", "t-003", "t-004"])
    before = dump_members(db_path, pid)
    ids = _live_item_ids(client, pid)
    kinds_before = _event_kinds(db_path)
    old_etag = etag
    r = _move(
        client,
        pid,
        etag,
        {
            "range_start": ids[1],
            "range_length": 2,
            "range_end": ids[2],
            "after_item_id": ids[0],
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["renumbered"] is False
    assert r.headers["ETag"] == old_etag
    assert dump_members(db_path, pid) == before
    assert _event_kinds(db_path) == kinds_before


def test_undo_restores_order_keys(
    client: TestClient,
    db_path: Path,
) -> None:
    body, etag = _create(client)
    pid = body["playlist_id"]
    etag = _put_tracks(client, pid, etag, ["t-001", "t-002", "t-003", "t-004", "t-005"])
    before = dump_members(db_path, pid)
    ids = _live_item_ids(client, pid)
    r = _move(
        client,
        pid,
        etag,
        {
            "range_start": ids[1],
            "range_length": 2,
            "range_end": ids[2],
            "after_item_id": ids[-1],
        },
    )
    assert r.status_code == 200, r.text
    undo = client.post("/api/v1/playlist-history/undo")
    assert undo.status_code == 200, undo.text
    after = dump_members(db_path, pid)
    for iid in before:
        live = after[iid]
        pre = before[iid]
        assert (live[0], live[1], live[2], live[3]) == (
            pre[0],
            pre[1],
            pre[2],
            pre[3],
        )
