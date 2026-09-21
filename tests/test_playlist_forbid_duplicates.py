"""LIBM-D2 / LIBM-03 / LIBM-04b: per-playlist forbid_duplicates policy.

[if] forbid_duplicates is on, a dup is added/toggled/copied [then] API rejects it, [else stop].
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
from apps.webui.server.playlist_dupes import first_repeated_stable_id, new_stable_ids
from apps.webui.server.routes import playlist_write
from apps.webui.server.sqlite_backend import SqliteBackend

TRACK_IDS: list[str] = ["t-001", "t-002", "t-003"]

_BPM_RULE: dict = {
    "op": "and",
    "children": [
        {"field": "bpm", "op": ">=", "value": 120},
        {"field": "bpm", "op": "<=", "value": 130},
    ],
}

pytestmark = [pytest.mark.requirement("LIBM-03"), pytest.mark.requirement("LIBM-04b")]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
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


def _create(client: TestClient, name: str = "Set") -> tuple[dict, str]:
    r = client.post("/api/v1/playlists", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json(), r.headers["ETag"]


def _put(client: TestClient, pid: str, etag: str, ids: list[str]) -> str:
    r = client.put(
        f"/api/v1/playlists/{pid}/tracks",
        json={"stable_ids": ids},
        headers={"If-Match": etag},
    )
    return r.status_code, r.headers.get("ETag"), r


def _patch_flag(client: TestClient, pid: str, etag: str, on: bool) -> tuple[str, dict]:
    r = client.patch(
        f"/api/v1/playlists/{pid}",
        json={"forbid_duplicates": on},
        headers={"If-Match": etag},
    )
    assert r.status_code == 200, r.text
    return r.headers["ETag"], r.json()


def dump_members(db_path: Path, playlist_id: str) -> list[tuple]:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT item_id, stable_id FROM playlist_memberships "
            "WHERE playlist_id=? AND deleted_at IS NULL "
            "ORDER BY COALESCE(order_key, printf('%08d', position)), position",
            (playlist_id,),
        ).fetchall()
    finally:
        conn.close()


def test_new_stable_ids_helpers() -> None:
    """[if] helpers filter request ids [then] only genuinely new ids remain, [else stop]."""
    assert new_stable_ids(["a"], ["a", "b", "a"]) == ["b"]
    assert new_stable_ids([], ["a", "a", "b"]) == ["a", "b"]
    assert first_repeated_stable_id(["a", "b", "a"]) == "a"
    assert first_repeated_stable_id(["a", "b"]) is None


def test_default_allow_duplicate_memberships(
    client: TestClient, db_path: Path,
) -> None:
    """[if] duplicates are allowed [then] two independent rows are stored, [else stop]."""
    body, etag = _create(client)
    pid = body["playlist_id"]
    status, etag, _put_r = _put(client, pid, etag, ["t-001"])
    assert status == 200
    add = client.post(
        f"/api/v1/playlists/{pid}/items:add",
        json={"stable_ids": ["t-001"]},
    )
    assert add.status_code == 200, add.text
    assert add.json()["forbid_duplicates"] is False
    members = dump_members(db_path, pid)
    assert len(members) == 2
    assert members[0][1] == members[1][1] == "t-001"
    assert members[0][0] != members[1][0]
    item0 = members[0][0]
    rm = client.delete(f"/api/v1/playlists/{pid}/items/{item0}")
    assert rm.status_code == 200
    assert len(dump_members(db_path, pid)) == 1
    track = client.get("/api/v1/tracks/t-001")
    assert track.status_code == 200
    assert track.json()["stable_id"] == "t-001"


def test_flag_on_add_is_noop(client: TestClient, db_path: Path) -> None:
    """[if] forbid_duplicates is on [then] re-adding an existing track is a no-op, [else stop]."""
    body, etag = _create(client)
    pid = body["playlist_id"]
    _, etag, _ = _put(client, pid, etag, ["t-001"])
    etag, _ = _patch_flag(client, pid, etag, True)
    before = dump_members(db_path, pid)
    old_etag = etag
    add = client.post(
        f"/api/v1/playlists/{pid}/items:add",
        json={"stable_ids": ["t-001"]},
    )
    assert add.status_code == 200, add.text
    assert dump_members(db_path, pid) == before
    assert add.headers.get("ETag") == old_etag


def test_flag_on_mixed_batch(client: TestClient, db_path: Path) -> None:
    """[if] a mixed batch is added with the flag on [then] only new ids insert, [else stop]."""
    body, etag = _create(client)
    pid = body["playlist_id"]
    _, etag, _ = _put(client, pid, etag, ["t-001"])
    etag, _ = _patch_flag(client, pid, etag, True)
    add = client.post(
        f"/api/v1/playlists/{pid}/items:add",
        json={"stable_ids": ["t-001", "t-002", "t-001"]},
    )
    assert add.status_code == 200, add.text
    members = dump_members(db_path, pid)
    assert [m[1] for m in members] == ["t-001", "t-002"]


def test_flag_on_repeated_body_single_row(client: TestClient, db_path: Path) -> None:
    """[if] an empty playlist gets a repeated body with the flag on [then] one row is stored, [else stop]."""
    body, etag = _create(client)
    pid = body["playlist_id"]
    etag, _ = _patch_flag(client, pid, etag, True)
    add = client.post(
        f"/api/v1/playlists/{pid}/items:add",
        json={"stable_ids": ["t-001", "t-001"]},
    )
    assert add.status_code == 200, add.text
    assert len(dump_members(db_path, pid)) == 1


def test_flag_on_put_rejects_duplicates(client: TestClient, db_path: Path) -> None:
    """[if] PUT body repeats an id with the flag on [then] 409 already_exists, [else stop]."""
    body, etag = _create(client)
    pid = body["playlist_id"]
    etag, _ = _patch_flag(client, pid, etag, True)
    status, _, r = _put(client, pid, etag, ["t-001", "t-001"])
    assert status == 409
    assert r.json()["error"] == "already_exists"
    assert dump_members(db_path, pid) == []


def test_flag_on_put_unique_ok(client: TestClient, db_path: Path) -> None:
    """[if] PUT body is unique with the flag on [then] 200 with two rows, [else stop]."""
    body, etag = _create(client)
    pid = body["playlist_id"]
    etag, _ = _patch_flag(client, pid, etag, True)
    status, _, r = _put(client, pid, etag, ["t-001", "t-002"])
    assert status == 200, r.text
    assert len(dump_members(db_path, pid)) == 2


def test_toggle_does_not_collapse_extras(client: TestClient, db_path: Path) -> None:
    """[if] duplicates exist before the flag turns on [then] both rows remain, [else stop]."""
    body, etag = _create(client)
    pid = body["playlist_id"]
    _, etag, _ = _put(client, pid, etag, ["t-001"])
    add = client.post(
        f"/api/v1/playlists/{pid}/items:add",
        json={"stable_ids": ["t-001"]},
    )
    assert add.status_code == 200
    etag = add.headers["ETag"]
    members = dump_members(db_path, pid)
    assert len(members) == 2
    etag, _ = _patch_flag(client, pid, etag, True)
    assert dump_members(db_path, pid) == members


def test_duplicate_playlist_copies_flag(client: TestClient, db_path: Path) -> None:
    """[if] a flagged playlist is duplicated [then] the copy inherits forbid_duplicates, [else stop]."""
    body, etag = _create(client)
    pid = body["playlist_id"]
    _, etag, _ = _put(client, pid, etag, ["t-001"])
    etag, _ = _patch_flag(client, pid, etag, True)
    dup = client.post(f"/api/v1/playlists/{pid}/duplicate")
    assert dup.status_code == 201, dup.text
    copy_id = dup.json()["playlist_id"]
    assert dup.json()["forbid_duplicates"] is True
    add = client.post(
        f"/api/v1/playlists/{copy_id}/items:add",
        json={"stable_ids": ["t-001"]},
    )
    assert add.status_code == 200
    assert len(dump_members(db_path, copy_id)) == 1


def test_get_list_and_detail_expose_flag(client: TestClient) -> None:
    """[if] the flag is patched on [then] list and detail expose forbid_duplicates, [else stop]."""
    body, etag = _create(client)
    pid = body["playlist_id"]
    etag, _ = _patch_flag(client, pid, etag, True)
    listed = client.get("/api/v1/playlists")
    assert listed.status_code == 200
    summary = next(p for p in listed.json() if p["playlist_id"] == pid)
    assert summary["forbid_duplicates"] is True
    detail = client.get(f"/api/v1/playlists/{pid}")
    assert detail.status_code == 200
    assert detail.json()["forbid_duplicates"] is True


def test_patch_flag_requires_if_match(client: TestClient) -> None:
    """[if] PATCH omits If-Match [then] 428 precondition_required, [else stop]."""
    body, _ = _create(client)
    pid = body["playlist_id"]
    r = client.patch(
        f"/api/v1/playlists/{pid}",
        json={"forbid_duplicates": True},
    )
    assert r.status_code == 428


def test_patch_flag_stale_etag(client: TestClient) -> None:
    """[if] PATCH uses a stale If-Match [then] 409 conflict, [else stop]."""
    body, _etag = _create(client)
    pid = body["playlist_id"]
    r = client.patch(
        f"/api/v1/playlists/{pid}",
        json={"forbid_duplicates": True},
        headers={"If-Match": '"stale"'},
    )
    assert r.status_code == 409


def test_smartlist_add_still_immutable(client: TestClient, db_path: Path) -> None:
    """[if] :add targets a smartlist [then] 422 smartlist_immutable, [else stop]."""
    conn = sqlite3.connect(str(db_path))
    try:
        sl_id = SmartlistsRepo(conn).create("120s", _BPM_RULE).id
        conn.commit()
    finally:
        conn.close()
    r = client.post(
        f"/api/v1/playlists/{sl_id}/items:add",
        json={"stable_ids": ["t-001"]},
    )
    assert r.status_code == 422
    assert r.json()["error"] == "smartlist_immutable"


def test_reverse_lookup_lists_playlist_once(client: TestClient, db_path: Path) -> None:
    """[if] two memberships exist [then] reverse lookup lists the playlist once, [else stop]."""
    body, etag = _create(client)
    pid = body["playlist_id"]
    _, etag, _ = _put(client, pid, etag, ["t-001"])
    client.post(f"/api/v1/playlists/{pid}/items:add", json={"stable_ids": ["t-001"]})
    r = client.get("/api/v1/tracks/t-001/playlists")
    assert r.status_code == 200
    hits = [h for h in r.json() if h["playlist_id"] == pid]
    assert len(hits) == 1
    assert len(hits[0]["positions"]) == 2
