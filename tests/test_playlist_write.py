"""Playlist WRITE contract tests (playlists-router gating unit).

Regression one-liners:
  * if POST /playlists does not create a row readable via GET /playlists then broken
  * if PATCH/DELETE/PUT succeed without If-Match then broken (must 428)
  * if a stale If-Match does not 409 with {current, etag} then broken
  * if PUT .../tracks does not fully replace membership order then broken
  * if replaying the same PUT list changes the etag then broken (idempotency)
  * if a membership change does not rotate the etag then broken (lost-update guard)
  * if writes skip the shared-state events log then broken (provenance)
  * if unknown stable_ids are partially written instead of 422 then broken
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server.app import create_app
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


@pytest.fixture
def client(db_path: Path) -> Iterator[TestClient]:
    app = create_app(
        backend=SqliteBackend(db_path), state_db_path=str(db_path),
        bind_host="127.0.0.1", hostname="test-host",
        lock_status_fn=lambda: None, mount_frontend=False,
    )
    # The integrator wires this into app.py; tests wire it explicitly so the
    # hotspot file stays untouched.
    app.include_router(playlist_write.router, prefix="/api/v1")
    with TestClient(app) as c:
        yield c
    playlist_write.close_store(app)


def _create(client: TestClient, name: str = "My Set") -> tuple[dict, str]:
    r = client.post("/api/v1/playlists", json={"name": name})
    assert r.status_code == 201, r.text
    assert r.headers.get("ETag")
    return r.json(), r.headers["ETag"]


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


# --- create ----------------------------------------------------------------

def test_create_playlist_round_trips_through_read_api(client: TestClient) -> None:
    body, etag = _create(client, "Warmup")
    assert body["name"] == "Warmup"
    assert body["vendor"] == "webui"
    assert body["items"] == []
    assert body["track_count"] == 0
    listed = client.get("/api/v1/playlists").json()
    assert any(p["playlist_id"] == body["playlist_id"] for p in listed)


def test_create_playlist_empty_name_422(client: TestClient) -> None:
    r = client.post("/api/v1/playlists", json={"name": ""})
    assert r.status_code == 422
    r2 = client.post("/api/v1/playlists", json={"name": "   "})
    assert r2.status_code == 422
    assert r2.json()["error"] == "invalid_patch"


# --- rename ----------------------------------------------------------------

def test_rename_requires_if_match(client: TestClient) -> None:
    body, _ = _create(client)
    r = client.patch(
        f"/api/v1/playlists/{body['playlist_id']}", json={"name": "X"},
    )
    assert r.status_code == 428
    assert r.json()["error"] == "precondition_required"


def test_rename_happy_path_rotates_etag(client: TestClient) -> None:
    body, etag = _create(client, "Old Name")
    r = client.patch(
        f"/api/v1/playlists/{body['playlist_id']}",
        json={"name": "New Name"}, headers={"If-Match": etag},
    )
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "New Name"
    assert r.headers["ETag"] != etag


def test_rename_with_stale_etag_409_with_current(client: TestClient) -> None:
    body, etag = _create(client, "Original")
    pid = body["playlist_id"]
    ok = client.patch(f"/api/v1/playlists/{pid}", json={"name": "Renamed"},
                      headers={"If-Match": etag})
    assert ok.status_code == 200
    stale = client.patch(f"/api/v1/playlists/{pid}", json={"name": "Again"},
                         headers={"If-Match": etag})
    assert stale.status_code == 409
    conflict = stale.json()
    assert conflict["error"] == "conflict"
    assert conflict["current"]["name"] == "Renamed"
    assert conflict["etag"] == stale.headers["ETag"]
    assert conflict["etag"] == ok.headers["ETag"]


def test_rename_omitted_name_is_noop_echo(client: TestClient) -> None:
    body, etag = _create(client, "Keep Me")
    r = client.patch(f"/api/v1/playlists/{body['playlist_id']}", json={},
                     headers={"If-Match": etag})
    assert r.status_code == 200
    assert r.json()["name"] == "Keep Me"
    assert r.headers["ETag"] == etag


def test_rename_unknown_playlist_404(client: TestClient) -> None:
    r = client.patch("/api/v1/playlists/nope", json={"name": "X"},
                     headers={"If-Match": '"whatever"'})
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


# --- delete ----------------------------------------------------------------

def test_delete_requires_if_match(client: TestClient) -> None:
    body, _ = _create(client)
    r = client.delete(f"/api/v1/playlists/{body['playlist_id']}")
    assert r.status_code == 428


def test_delete_with_stale_etag_409(client: TestClient) -> None:
    body, etag = _create(client)
    pid = body["playlist_id"]
    client.patch(f"/api/v1/playlists/{pid}", json={"name": "Moved On"},
                 headers={"If-Match": etag})
    r = client.delete(f"/api/v1/playlists/{pid}", headers={"If-Match": etag})
    assert r.status_code == 409


def test_delete_happy_path_then_404(client: TestClient, db_path: Path) -> None:
    body, etag = _create(client, "Doomed")
    pid = body["playlist_id"]
    r = client.delete(f"/api/v1/playlists/{pid}", headers={"If-Match": etag})
    assert r.status_code == 204
    assert client.get(f"/api/v1/playlists/{pid}").status_code == 404
    assert "playlist.delete" in _event_kinds(db_path)


# --- duplicate -------------------------------------------------------------

def test_duplicate_copies_name_and_membership(client: TestClient) -> None:
    body, etag = _create(client, "Peak Hour")
    pid = body["playlist_id"]
    put = client.put(f"/api/v1/playlists/{pid}/tracks",
                     json={"stable_ids": ["t-002", "t-001"]},
                     headers={"If-Match": etag})
    assert put.status_code == 200, put.text
    dup = client.post(f"/api/v1/playlists/{pid}/duplicate")
    assert dup.status_code == 201, dup.text
    copy = dup.json()
    assert copy["playlist_id"] != pid
    assert copy["name"] == "Peak Hour (copy)"
    assert copy["items"] == ["t-002", "t-001"]
    assert dup.headers.get("ETag")


def test_duplicate_with_explicit_name_and_stale_source_etag(client: TestClient) -> None:
    body, etag = _create(client, "Source")
    pid = body["playlist_id"]
    named = client.post(f"/api/v1/playlists/{pid}/duplicate",
                        json={"name": "Fork A"})
    assert named.status_code == 201
    assert named.json()["name"] == "Fork A"
    # Rotate the source's etag, then duplicate against the stale one -> 409.
    client.patch(f"/api/v1/playlists/{pid}", json={"name": "Rotated"},
                 headers={"If-Match": etag})
    stale = client.post(f"/api/v1/playlists/{pid}/duplicate",
                        headers={"If-Match": etag})
    assert stale.status_code == 409


def test_duplicate_unknown_playlist_404(client: TestClient) -> None:
    r = client.post("/api/v1/playlists/nope/duplicate")
    assert r.status_code == 404


# --- membership replace ----------------------------------------------------

def test_put_tracks_requires_if_match(client: TestClient) -> None:
    body, _ = _create(client)
    r = client.put(f"/api/v1/playlists/{body['playlist_id']}/tracks",
                   json={"stable_ids": ["t-001"]})
    assert r.status_code == 428


def test_put_tracks_full_replace_round_trip(client: TestClient) -> None:
    body, etag = _create(client, "Builder")
    pid = body["playlist_id"]
    r1 = client.put(f"/api/v1/playlists/{pid}/tracks",
                    json={"stable_ids": ["t-001", "t-002", "t-003"]},
                    headers={"If-Match": etag})
    assert r1.status_code == 200, r1.text
    assert r1.json()["items"] == ["t-001", "t-002", "t-003"]
    assert r1.headers["ETag"] != etag  # membership change rotates the etag
    # Reorder + drop + add in ONE replace.
    r2 = client.put(f"/api/v1/playlists/{pid}/tracks",
                    json={"stable_ids": ["t-004", "t-002"]},
                    headers={"If-Match": r1.headers["ETag"]})
    assert r2.status_code == 200
    assert r2.json()["items"] == ["t-004", "t-002"]
    # Read side sees the same order, hydrated.
    detail = client.get(f"/api/v1/playlists/{pid}").json()
    assert detail["items"] == ["t-004", "t-002"]
    assert [t["stable_id"] for t in detail["tracks"]] == ["t-004", "t-002"]


def test_put_tracks_idempotent_replay_keeps_etag(client: TestClient) -> None:
    body, etag = _create(client)
    pid = body["playlist_id"]
    r1 = client.put(f"/api/v1/playlists/{pid}/tracks",
                    json={"stable_ids": ["t-003", "t-001"]},
                    headers={"If-Match": etag})
    e1 = r1.headers["ETag"]
    r2 = client.put(f"/api/v1/playlists/{pid}/tracks",
                    json={"stable_ids": ["t-003", "t-001"]},
                    headers={"If-Match": e1})
    assert r2.status_code == 200
    assert r2.headers["ETag"] == e1
    assert r2.json()["items"] == ["t-003", "t-001"]


def test_put_tracks_stale_etag_409(client: TestClient) -> None:
    body, etag = _create(client)
    pid = body["playlist_id"]
    r1 = client.put(f"/api/v1/playlists/{pid}/tracks",
                    json={"stable_ids": ["t-001"]}, headers={"If-Match": etag})
    assert r1.status_code == 200
    stale = client.put(f"/api/v1/playlists/{pid}/tracks",
                       json={"stable_ids": ["t-002"]},
                       headers={"If-Match": etag})
    assert stale.status_code == 409
    assert stale.json()["current"]["items"] == ["t-001"]


def test_put_tracks_unknown_stable_id_422_writes_nothing(client: TestClient) -> None:
    body, etag = _create(client)
    pid = body["playlist_id"]
    r = client.put(f"/api/v1/playlists/{pid}/tracks",
                   json={"stable_ids": ["t-001", "t-ghost"]},
                   headers={"If-Match": etag})
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_patch"
    assert "t-ghost" in r.json()["message"]
    # Nothing was partially written and the etag did not rotate.
    detail = client.get(f"/api/v1/playlists/{pid}").json()
    assert detail["items"] == []


def test_put_tracks_allows_duplicate_members(client: TestClient) -> None:
    body, etag = _create(client)
    pid = body["playlist_id"]
    r = client.put(f"/api/v1/playlists/{pid}/tracks",
                   json={"stable_ids": ["t-001", "t-002", "t-001"]},
                   headers={"If-Match": etag})
    assert r.status_code == 200, r.text
    assert r.json()["items"] == ["t-001", "t-002", "t-001"]


# --- provenance / events ---------------------------------------------------

def test_full_crud_appends_events_with_actor(client: TestClient, db_path: Path) -> None:
    body, etag = _create(client, "Audit Me")
    pid = body["playlist_id"]
    r = client.put(f"/api/v1/playlists/{pid}/tracks",
                   json={"stable_ids": ["t-001"]}, headers={"If-Match": etag})
    client.delete(f"/api/v1/playlists/{pid}",
                  headers={"If-Match": r.headers["ETag"]})
    kinds = _event_kinds(db_path)
    for expected in ("playlist.insert", "playlist.memberships.set",
                     "playlist.delete"):
        assert expected in kinds, f"missing event kind {expected} in {kinds}"
    conn = sqlite3.connect(str(db_path))
    try:
        actors = {
            row[0] for row in conn.execute(
                "SELECT DISTINCT actor FROM events "
                "WHERE kind LIKE 'playlist.%'"
            )
        }
    finally:
        conn.close()
    assert actors == {"webui"}
