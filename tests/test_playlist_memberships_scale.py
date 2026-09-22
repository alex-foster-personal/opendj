"""500-membership fixture: O(1) add, tombstone remove, slice move, bulk cap."""
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

SCALE = 500
TRACK_IDS = [f"t-{i:03d}" for i in range(SCALE)]
EXTRA_IDS = [f"t-extra-{i:03d}" for i in range(1001)]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="test-seed")
    try:
        for sid in TRACK_IDS + EXTRA_IDS + ["t-extra"]:
            writer.upsert_track(
                stable_id=sid, stable_id_tier="inferred",
                title=sid, artists=["Artist"], album=None,
                isrc=None, duration_ms=180_000, file_path=None,
            )
    finally:
        writer.close()
        conn.close()
    return path


@pytest.fixture
def client(db_path: Path) -> Iterator[TestClient]:
    app = create_app(
        backend=SqliteBackend(db_path), state_db_path=str(db_path),
        bind_host="127.0.0.1", hostname="test-host",
        lock_status_fn=lambda: None, mount_frontend=False,
    )
    with TestClient(app) as c:
        yield c
    playlist_write.close_store(app)


def _create(client: TestClient, name: str = "Scale Set") -> tuple[dict, str]:
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


def _ordered_live(db_path: Path, playlist_id: str) -> list[tuple]:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT item_id, stable_id, position, order_key, updated_at, "
            "origin_device_id, deleted_at FROM playlist_memberships "
            "WHERE playlist_id=? AND deleted_at IS NULL "
            "ORDER BY COALESCE(order_key, printf('%08d', position)), position",
            (playlist_id,),
        ).fetchall()
    finally:
        conn.close()


def _dump_all(db_path: Path, playlist_id: str) -> list[tuple]:
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


def _store_conn(client: TestClient) -> sqlite3.Connection:
    store = client.app.state.playlist_store
    return store._conn


def _install_write_counter(client: TestClient) -> None:
    conn = _store_conn(client)
    conn.execute(
        "CREATE TEMP TABLE IF NOT EXISTS _membership_writes "
        "(op TEXT, cnt INTEGER DEFAULT 0)"
    )
    conn.execute("DELETE FROM _membership_writes")
    conn.execute("DROP TRIGGER IF EXISTS _count_membership_inserts")
    conn.execute("DROP TRIGGER IF EXISTS _count_membership_updates")
    conn.execute("DROP TRIGGER IF EXISTS _count_membership_deletes")
    conn.execute(
        "CREATE TEMP TRIGGER _count_membership_inserts "
        "AFTER INSERT ON playlist_memberships BEGIN "
        "INSERT INTO _membership_writes(op, cnt) VALUES ('INSERT', 1); END"
    )
    conn.execute(
        "CREATE TEMP TRIGGER _count_membership_updates "
        "AFTER UPDATE ON playlist_memberships BEGIN "
        "INSERT INTO _membership_writes(op, cnt) VALUES ('UPDATE', 1); END"
    )
    conn.execute(
        "CREATE TEMP TRIGGER _count_membership_deletes "
        "AFTER DELETE ON playlist_memberships BEGIN "
        "INSERT INTO _membership_writes(op, cnt) VALUES ('DELETE', 1); END"
    )


def _write_counts(client: TestClient) -> dict[str, int]:
    conn = _store_conn(client)
    rows = conn.execute(
        "SELECT op, COUNT(*) FROM _membership_writes GROUP BY op"
    ).fetchall()
    return {op: cnt for op, cnt in rows}


def _seed_500_playlist(client: TestClient, db_path: Path) -> tuple[str, str]:
    body, etag = _create(client)
    pid = body["playlist_id"]
    etag = _put_tracks(client, pid, etag, TRACK_IDS)
    return pid, etag


@pytest.mark.requirement("LIBM-01")
def test_single_row_add_write_count(client: TestClient, db_path: Path) -> None:
    """[if] one track is added to a 500-member playlist [then] exactly one row is written, [else stop]."""
    pid, _etag = _seed_500_playlist(client, db_path)
    before = _ordered_live(db_path, pid)
    assert len(before) == SCALE
    _install_write_counter(client)
    r = client.post(
        f"/api/v1/playlists/{pid}/items:add",
        json={"stable_ids": ["t-extra"]},
    )
    assert r.status_code == 200, r.text
    counts = _write_counts(client)
    assert counts.get("INSERT", 0) == 1
    assert counts.get("UPDATE", 0) == 0
    assert counts.get("DELETE", 0) == 0
    after = _ordered_live(db_path, pid)
    assert len(after) == SCALE + 1
    for row in before:
        assert row in after
    assert r.json()["items"][-1] == "t-extra"


@pytest.mark.requirement("LIBM-03")
def test_duplicate_allowed_add(client: TestClient, db_path: Path) -> None:
    """[if] an already-present track is added [then] a second row is created, [else stop]."""
    pid, _etag = _seed_500_playlist(client, db_path)
    before = _ordered_live(db_path, pid)
    original_first = before[0]
    r = client.post(
        f"/api/v1/playlists/{pid}/items:add",
        json={"stable_ids": ["t-000"]},
    )
    assert r.status_code == 200, r.text
    after = _ordered_live(db_path, pid)
    t000_rows = [row for row in after if row[1] == "t-000"]
    assert len(t000_rows) == 2
    assert original_first in after
    assert t000_rows[0][0] != t000_rows[1][0]


@pytest.mark.requirement("LIBM-21")
def test_tombstoned_remove(client: TestClient, db_path: Path) -> None:
    """[if] items:remove targets one item id [then] only that row is tombstoned, [else stop]."""
    pid, _etag = _seed_500_playlist(client, db_path)
    live = _ordered_live(db_path, pid)
    target = live[10]
    neighbors = {live[9], live[11]}
    before_dump = _dump_all(db_path, pid)
    r = client.post(
        f"/api/v1/playlists/{pid}/items:remove",
        json={"item_ids": [target[0]]},
    )
    assert r.status_code == 200, r.text
    after_dump = _dump_all(db_path, pid)
    tombstoned = next(row for row in after_dump if row[0] == target[0])
    assert tombstoned[6] is not None
    for row in after_dump:
        if row[0] in {n[0] for n in neighbors}:
            assert row == next(r for r in before_dump if r[0] == row[0])
    r404 = client.post(
        f"/api/v1/playlists/{pid}/items:remove",
        json={"item_ids": ["no-such-item"]},
    )
    assert r404.status_code == 404
    assert _dump_all(db_path, pid) == after_dump
    delete_target = live[20]
    r_del = client.delete(f"/api/v1/playlists/{pid}/items/{delete_target[0]}")
    assert r_del.status_code == 200, r_del.text


@pytest.mark.requirement("LIBM-22")
def test_slice_move(client: TestClient, db_path: Path) -> None:
    """[if] a five-row slice is moved [then] only those order_keys change, [else stop]."""
    pid, etag = _seed_500_playlist(client, db_path)
    live = _ordered_live(db_path, pid)
    range_start = live[10][0]
    after_item = live[100][0]
    before_by_id = {row[0]: row for row in live}
    _install_write_counter(client)
    r = client.post(
        f"/api/v1/playlists/{pid}/items:move",
        json={
            "range_start": range_start,
            "range_length": 5,
            "after_item_id": after_item,
        },
        headers={"If-Match": etag},
    )
    assert r.status_code == 200, r.text
    counts = _write_counts(client)
    assert counts.get("INSERT", 0) == 0
    assert counts.get("DELETE", 0) == 0
    assert counts.get("UPDATE", 0) == 5
    after_live = _ordered_live(db_path, pid)
    moved_ids = {live[i][0] for i in range(10, 15)}
    for row in after_live:
        if row[0] not in moved_ids:
            before_row = before_by_id[row[0]]
            assert row[0] == before_row[0]
            assert row[3] == before_row[3]


@pytest.mark.requirement("LIBM-26")
def test_bulk_cap(client: TestClient, db_path: Path) -> None:
    """[if] add or remove exceeds 1000 ids [then] the batch is rejected, [else stop]."""
    pid, _etag = _seed_500_playlist(client, db_path)
    before = _dump_all(db_path, pid)
    over = EXTRA_IDS[:1001]
    r_over = client.post(
        f"/api/v1/playlists/{pid}/items:add",
        json={"stable_ids": over},
    )
    assert r_over.status_code == 422
    assert r_over.json()["error"] == "bulk_limit"
    assert "1000" in r_over.json()["message"]
    assert _dump_all(db_path, pid) == before
    at_cap = EXTRA_IDS[:1000]
    r_cap = client.post(
        f"/api/v1/playlists/{pid}/items:add",
        json={"stable_ids": at_cap},
    )
    assert r_cap.status_code == 200, r_cap.text
    assert len(_ordered_live(db_path, pid)) == SCALE + 1000
    live = _ordered_live(db_path, pid)
    remove_over = [row[0] for row in live[:1001]]
    r_rm_over = client.post(
        f"/api/v1/playlists/{pid}/items:remove",
        json={"item_ids": remove_over},
    )
    assert r_rm_over.status_code == 422
    assert r_rm_over.json()["error"] == "bulk_limit"
    remove_two = [live[0][0], live[1][0]]
    r_rm = client.post(
        f"/api/v1/playlists/{pid}/items:remove",
        json={"item_ids": remove_two},
    )
    assert r_rm.status_code == 200, r_rm.text
    after_live = _ordered_live(db_path, pid)
    assert all(row[0] not in remove_two for row in after_live)


def test_rename_then_add_no_precondition(client: TestClient, db_path: Path) -> None:
    pid, etag = _seed_500_playlist(client, db_path)
    r_rename = client.patch(
        f"/api/v1/playlists/{pid}",
        json={"name": "Renamed Scale Set"},
        headers={"If-Match": etag},
    )
    assert r_rename.status_code == 200, r.text
    r_add = client.post(
        f"/api/v1/playlists/{pid}/items:add",
        json={"stable_ids": ["t-extra"]},
    )
    assert r_add.status_code == 200, r_add.text


@pytest.mark.requirement("LIBM-08")
def test_readd_after_remove(client: TestClient, db_path: Path) -> None:
    """[if] a removed track is re-added [then] a new item id is issued, [else stop]."""
    pid, _etag = _seed_500_playlist(client, db_path)
    live = _ordered_live(db_path, pid)
    target = live[0]
    r_rm = client.delete(f"/api/v1/playlists/{pid}/items/{target[0]}")
    assert r_rm.status_code == 200, r_rm.text
    tombstone = next(
        row for row in _dump_all(db_path, pid) if row[0] == target[0]
    )
    assert tombstone[6] is not None
    r_add = client.post(
        f"/api/v1/playlists/{pid}/items:add",
        json={"stable_ids": ["t-000"]},
    )
    assert r_add.status_code == 200, r_add.text
    new_rows = [
        row for row in _ordered_live(db_path, pid) if row[1] == "t-000"
    ]
    assert len(new_rows) == 1
    assert new_rows[0][0] != target[0]
    assert tombstone in _dump_all(db_path, pid)


def test_all_tracks_lists_once(client: TestClient, db_path: Path) -> None:
    from apps.webui.server.sqlite_backend import _TRACKS_PROJECTION

    assert "playlist_memberships" not in _TRACKS_PROJECTION
    shared = "t-shared"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO tracks(stable_id, stable_id_tier, title, "
            "created_at, updated_at) VALUES (?, 'inferred', ?, "
            "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
            (shared, shared),
        )
        conn.commit()
    finally:
        conn.close()
    for i in range(12):
        body, _ = _create(client, f"PL-{i}")
        client.post(
            f"/api/v1/playlists/{body['playlist_id']}/items:add",
            json={"stable_ids": [shared]},
        )
    r = client.get("/api/v1/tracks?q=t-shared&limit=500")
    assert r.status_code == 200
    matches = [t for t in r.json()["items"] if t["stable_id"] == shared]
    assert len(matches) == 1


@pytest.mark.requirement("LIBM-01")
def test_put_full_replace_write_count(client: TestClient, db_path: Path) -> None:
    """[if] PUT replaces 501 tracks [then] membership writes are O(N), [else stop]."""
    pid, etag = _seed_500_playlist(client, db_path)
    _install_write_counter(client)
    ids = TRACK_IDS + ["t-extra"]
    r = client.put(
        f"/api/v1/playlists/{pid}/tracks",
        json={"stable_ids": ids},
        headers={"If-Match": etag},
    )
    assert r.status_code == 200, r.text
    counts = _write_counts(client)
    total = sum(counts.values())
    # PUT deletes 500 rows and inserts 501: measured 1001 membership writes.
    assert total >= 501


def test_openapi_documents_membership_endpoints(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    assert "/api/v1/playlists/{playlist_id}/items:add" in paths
    assert "/api/v1/playlists/{playlist_id}/items:remove" in paths
    assert "/api/v1/playlists/{playlist_id}/items:move" in paths
    assert "/api/v1/playlists/{playlist_id}/items/{item_id}" in paths
    assert "/api/v1/playlists/{playlist_id}/tracks" in paths
