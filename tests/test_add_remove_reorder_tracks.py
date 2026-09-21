"""add-remove-reorder-tracks node: GET-etag + PUT .../tracks round trip.

The membership primitive itself (PUT /playlists/{id}/tracks, full-list
replace = add/remove/reorder in one idempotent call) is already covered by
tests/test_playlist_write.py. This file covers the piece this node actually
added: GET /playlists/{id} now emits an ETag header (etag.py's
compute_etag(playlist_id, updated_at), identical to the write side's) so a
client that only reads through the hydrated detail route can still mutate
membership without a separate lookup -- and exercises the add / remove /
reorder flows a client performs with that etag, exactly as the frontend
does (fetch detail -> compute next stable_ids -> PUT with If-Match).

Regression one-liners:
  * if GET /playlists/{id} has no ETag header then broken
  * if that ETag does not match the etag a subsequent PUT accepts then broken
  * if append (add) does not preserve existing order then broken
  * if removing by position drops the wrong track then broken
  * if reorder does not persist through a re-read then broken
  * if a mutation against a stale GET-sourced etag does not 409 then broken
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server.app import create_app
from apps.webui.server.etag import compute_etag
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


@pytest.fixture
def client(db_path: Path) -> Iterator[TestClient]:
    # playlist_write + playlists (read) are both wired unconditionally by
    # create_app now (merged base branch) -- no manual include_router or
    # close_store needed, the app lifespan handles the latter.
    app = create_app(
        backend=SqliteBackend(db_path), state_db_path=str(db_path),
        bind_host="127.0.0.1", hostname="test-host",
        lock_status_fn=lambda: None, mount_frontend=False,
    )
    with TestClient(app) as c:
        yield c


def _create(client: TestClient, name: str = "My Set") -> str:
    r = client.post("/api/v1/playlists", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["playlist_id"]


def _get_with_etag(client: TestClient, pid: str) -> tuple[dict, str]:
    r = client.get(f"/api/v1/playlists/{pid}")
    assert r.status_code == 200, r.text
    assert r.headers.get("ETag"), "GET /playlists/{id} must carry an ETag header"
    return r.json(), r.headers["ETag"]


def _put_tracks(client: TestClient, pid: str, etag: str, stable_ids: list[str]):
    return client.put(
        f"/api/v1/playlists/{pid}/tracks",
        json={"stable_ids": stable_ids},
        headers={"If-Match": etag},
    )


# --- GET etag contract -------------------------------------------------

def test_get_playlist_etag_matches_computed_value(client: TestClient) -> None:
    pid = _create(client, "Etag Check")
    _, etag = _get_with_etag(client, pid)
    assert etag == compute_etag(pid, _updated_at(client, pid))


def _updated_at(client: TestClient, pid: str) -> str:
    # PlaylistDetail has no top-level updated_at; recover it via the
    # summary listing so the etag formula can be checked independently of
    # the route under test.
    r = client.get("/api/v1/playlists")
    row = next(p for p in r.json() if p["playlist_id"] == pid)
    return row["updated_at"]


def test_get_playlist_etag_accepted_by_put_tracks(client: TestClient) -> None:
    pid = _create(client, "Fresh From GET")
    _, etag = _get_with_etag(client, pid)
    r = _put_tracks(client, pid, etag, ["t-001"])
    assert r.status_code == 200, r.text
    assert r.json()["items"] == ["t-001"]


def test_get_playlist_etag_rotates_after_membership_change(client: TestClient) -> None:
    pid = _create(client, "Rotator")
    _, etag0 = _get_with_etag(client, pid)
    put = _put_tracks(client, pid, etag0, ["t-001"])
    assert put.status_code == 200
    _, etag1 = _get_with_etag(client, pid)
    assert etag1 != etag0
    assert etag1 == put.headers["ETag"]


# --- add / remove / reorder as the frontend performs them ---------------

def test_add_tracks_appends_preserving_existing_order(client: TestClient) -> None:
    pid = _create(client, "Warmup")
    detail, etag = _get_with_etag(client, pid)
    put1 = _put_tracks(client, pid, etag, ["t-001", "t-002"])
    assert put1.status_code == 200

    detail, etag = _get_with_etag(client, pid)
    next_ids = detail["items"] + ["t-003"]
    put2 = _put_tracks(client, pid, etag, next_ids)
    assert put2.status_code == 200
    assert put2.json()["items"] == ["t-001", "t-002", "t-003"]


def test_remove_track_by_position_drops_only_that_slot(client: TestClient) -> None:
    pid = _create(client, "Trim Me")
    detail, etag = _get_with_etag(client, pid)
    seeded = _put_tracks(client, pid, etag, ["t-001", "t-002", "t-001", "t-003"])
    assert seeded.status_code == 200

    detail, etag = _get_with_etag(client, pid)
    # Remove position 2 (0-based index 2, the SECOND "t-001") -- duplicate
    # stable_ids are allowed (djay/rekordbox parity), so removal must be
    # positional, not value-based.
    remove_index = 2
    next_ids = [sid for i, sid in enumerate(detail["items"]) if i != remove_index]
    result = _put_tracks(client, pid, etag, next_ids)
    assert result.status_code == 200
    assert result.json()["items"] == ["t-001", "t-002", "t-003"]


def test_reorder_drag_moves_track_and_persists(client: TestClient) -> None:
    pid = _create(client, "Reshuffle")
    detail, etag = _get_with_etag(client, pid)
    seeded = _put_tracks(client, pid, etag, ["t-001", "t-002", "t-003", "t-004"])
    assert seeded.status_code == 200

    detail, etag = _get_with_etag(client, pid)
    items = list(detail["items"])
    # Drag t-004 (index 3) to the front (drop onto index 0's original slot).
    moved = items.pop(3)
    items.insert(0, moved)
    result = _put_tracks(client, pid, etag, items)
    assert result.status_code == 200
    assert result.json()["items"] == ["t-004", "t-001", "t-002", "t-003"]

    # Reload confirms the order persisted (not just echoed in the response).
    reread, _ = _get_with_etag(client, pid)
    assert reread["items"] == ["t-004", "t-001", "t-002", "t-003"]
    assert [t["stable_id"] for t in reread["tracks"]] == [
        "t-004", "t-001", "t-002", "t-003",
    ]


def test_mutation_against_stale_get_etag_conflicts(client: TestClient) -> None:
    pid = _create(client, "Race")
    _, etag_a = _get_with_etag(client, pid)
    # A second reader also holds an etag from before any writes.
    _, etag_b = _get_with_etag(client, pid)
    assert etag_a == etag_b

    first = _put_tracks(client, pid, etag_a, ["t-001"])
    assert first.status_code == 200

    # The second reader's (now stale) GET-sourced etag must be rejected,
    # never silently overwrite the first writer's change.
    stale = _put_tracks(client, pid, etag_b, ["t-002"])
    assert stale.status_code == 409
    body = stale.json()
    assert body["error"] == "conflict"
    assert body["current"]["items"] == ["t-001"]
