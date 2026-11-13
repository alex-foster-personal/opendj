"""HTTP contract for /api/v1/playlist-history (playlists-router gating unit).

Regression one-liners:
  * if GET /playlist-history is not 200 with cursor/limit/can_undo/entries then broken
  * if POST undo/redo does not round-trip the playlist through GET /playlists/{id} then broken
  * if POST undo on an empty stack is not 409 nothing_to_undo then broken
  * if a sneaky live mutation does not 409 conflict on undo then broken
  * if GET /playlist-history without state.db is not 503 then broken
  * if redo onto a playlist changed since the undo is not a clean 409 conflict leaving live and history untouched then broken
  * if redo after a new edit dropped the redo entry is not 409 nothing_to_redo with live unchanged then broken
  * if a valid undo then redo does not restore the exact membership order then broken
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
from apps.webui.server.backend import InMemoryBackend
from apps.webui.server.playlist_store import PlaylistStore
from apps.webui.server.routes import playlist_write
from apps.webui.server.sqlite_backend import SqliteBackend

TRACK_IDS: list[str] = ["t-001", "t-002", "t-003", "t-004"]


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


def test_get_history_shape_empty_and_after_create(client: TestClient) -> None:
    empty = client.get("/api/v1/playlist-history")
    assert empty.status_code == 200, empty.text
    body = empty.json()
    assert body["cursor"] == 0
    assert body["limit"] == 50
    assert body["can_undo"] is False
    assert body["can_redo"] is False
    assert body["entries"] == []

    created, _etag = _create(client, "Warmup")
    hist = client.get("/api/v1/playlist-history").json()
    assert hist["can_undo"] is True
    assert hist["can_redo"] is False
    assert hist["cursor"] == 1
    assert len(hist["entries"]) == 1
    entry = hist["entries"][0]
    assert entry["op"] == "create"
    assert entry["playlist_id"] == created["playlist_id"]
    assert entry["label"] == "Create 'Warmup'"
    assert entry["command_id"]
    assert entry["ts"]


def test_post_undo_redo_round_trip_and_library_read(client: TestClient) -> None:
    body, etag = _create(client, "Warmup")
    pid = body["playlist_id"]
    put = client.put(
        f"/api/v1/playlists/{pid}/tracks",
        json={"stable_ids": ["t-002", "t-001"]},
        headers={"If-Match": etag},
    )
    assert put.status_code == 200, put.text
    renamed = client.patch(
        f"/api/v1/playlists/{pid}",
        json={"name": "Peak"},
        headers={"If-Match": put.headers["ETag"]},
    )
    assert renamed.status_code == 200

    undone = client.post("/api/v1/playlist-history/undo")
    assert undone.status_code == 200, undone.text
    undo_body = undone.json()
    assert undo_body["action"] == "undo"
    assert undo_body["op"] == "rename"
    assert undo_body["current"]["name"] == "Warmup"
    assert undo_body["current"]["items"] == ["t-002", "t-001"]
    assert undone.headers.get("ETag")
    assert client.get(f"/api/v1/playlists/{pid}").json()["name"] == "Warmup"

    undone_tracks = client.post("/api/v1/playlist-history/undo")
    assert undone_tracks.status_code == 200, undone_tracks.text
    assert undone_tracks.json()["current"]["items"] == []
    assert client.get(f"/api/v1/playlists/{pid}").json()["items"] == []

    redone = client.post("/api/v1/playlist-history/redo")
    assert redone.status_code == 200, redone.text
    assert redone.json()["action"] == "redo"
    assert redone.json()["op"] == "memberships"
    assert redone.json()["current"]["items"] == ["t-002", "t-001"]
    assert client.get(f"/api/v1/playlists/{pid}").json()["items"] == ["t-002", "t-001"]


def test_undo_create_returns_null_current(client: TestClient) -> None:
    body, _etag = _create(client, "Doomed")
    pid = body["playlist_id"]
    undone = client.post("/api/v1/playlist-history/undo")
    assert undone.status_code == 200, undone.text
    assert undone.json()["current"] is None
    assert "etag" not in undone.json() or undone.json()["etag"] is None
    assert client.get(f"/api/v1/playlists/{pid}").status_code == 404


def test_empty_stack_undo_redo_409(client: TestClient) -> None:
    undo = client.post("/api/v1/playlist-history/undo")
    assert undo.status_code == 409
    assert undo.json()["error"] == "nothing_to_undo"
    _create(client, "Only")
    redo = client.post("/api/v1/playlist-history/redo")
    assert redo.status_code == 409
    assert redo.json()["error"] == "nothing_to_redo"
    still = client.get("/api/v1/playlist-history")
    assert still.status_code == 200
    assert still.json()["can_undo"] is True


def test_conflict_409_when_live_diverges(client: TestClient, db_path: Path) -> None:
    body, etag = _create(client, "Warmup")
    pid = body["playlist_id"]
    put = client.put(
        f"/api/v1/playlists/{pid}/tracks",
        json={"stable_ids": ["t-001"]},
        headers={"If-Match": etag},
    )
    assert put.status_code == 200
    with PlaylistStore(db_path, bus=FakeEventBus()) as sneaky:
        sneaky.replace_memberships(
            pid,
            ["t-002"],
            expected_etag=put.headers["ETag"],
            record_edit=False,
        )
    conflict = client.post("/api/v1/playlist-history/undo")
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"] == "conflict"
    assert conflict.json()["current"]["items"] == ["t-002"]


def test_503_without_state_db(tmp_path: Path) -> None:
    missing = tmp_path / "nope" / "state.db"
    app = create_app(
        backend=InMemoryBackend(),
        state_db_path=str(missing),
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
        mount_frontend=False,
    )
    with TestClient(app) as c:
        r = c.get("/api/v1/playlist-history")
        assert r.status_code == 503
        assert r.json()["detail"]["error"] == "state_db_missing"
    playlist_write.close_store(app)


def test_stale_redo_409_conflict_leaves_live_and_history(
    client: TestClient,
    db_path: Path,
) -> None:
    """Redo onto a playlist revised since the undo refuses cleanly without mutating anything."""
    body, etag = _create(client, "Warmup")
    pid = body["playlist_id"]
    put = client.put(
        f"/api/v1/playlists/{pid}/tracks",
        json={"stable_ids": ["t-001"]},
        headers={"If-Match": etag},
    )
    assert put.status_code == 200, put.text
    undone = client.post("/api/v1/playlist-history/undo")
    assert undone.status_code == 200, undone.text
    with PlaylistStore(db_path, bus=FakeEventBus()) as sneaky:
        sneaky.replace_memberships(
            pid,
            ["t-003"],
            expected_etag=undone.headers["ETag"],
            record_edit=False,
        )
    history_before = client.get("/api/v1/playlist-history").json()
    assert history_before["can_redo"] is True

    conflict = client.post("/api/v1/playlist-history/redo")
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"] == "conflict"
    assert conflict.json()["current"]["items"] == ["t-003"]
    assert client.get(f"/api/v1/playlists/{pid}").json()["items"] == ["t-003"]
    assert client.get("/api/v1/playlist-history").json() == history_before


def test_redo_after_new_edit_409_nothing_to_redo(client: TestClient) -> None:
    """A fresh edit after undo drops the redo entry, so redo is a clean 409 with live unchanged."""
    body, etag = _create(client, "Warmup")
    pid = body["playlist_id"]
    renamed = client.patch(
        f"/api/v1/playlists/{pid}",
        json={"name": "Peak"},
        headers={"If-Match": etag},
    )
    assert renamed.status_code == 200, renamed.text
    undone = client.post("/api/v1/playlist-history/undo")
    assert undone.status_code == 200, undone.text
    assert client.get("/api/v1/playlist-history").json()["can_redo"] is True
    put = client.put(
        f"/api/v1/playlists/{pid}/tracks",
        json={"stable_ids": ["t-004", "t-002"]},
        headers={"If-Match": undone.headers["ETag"]},
    )
    assert put.status_code == 200, put.text
    history_before = client.get("/api/v1/playlist-history").json()
    assert history_before["can_redo"] is False
    live_before = client.get(f"/api/v1/playlists/{pid}").json()

    redo = client.post("/api/v1/playlist-history/redo")
    assert redo.status_code == 409, redo.text
    assert redo.json() == {"error": "nothing_to_redo"}
    assert redo.headers.get("ETag") is None
    assert client.get(f"/api/v1/playlists/{pid}").json() == live_before
    assert live_before["name"] == "Warmup"
    assert live_before["items"] == ["t-004", "t-002"]
    assert client.get("/api/v1/playlist-history").json() == history_before


def test_undo_redo_round_trip_restores_exact_order(client: TestClient) -> None:
    """Undo then redo of a reorder restores the exact non-sorted membership order."""
    body, etag = _create(client, "Warmup")
    pid = body["playlist_id"]
    first = client.put(
        f"/api/v1/playlists/{pid}/tracks",
        json={"stable_ids": ["t-001", "t-002", "t-003", "t-004"]},
        headers={"If-Match": etag},
    )
    assert first.status_code == 200, first.text
    reorder = ["t-003", "t-001", "t-004", "t-002"]
    second = client.put(
        f"/api/v1/playlists/{pid}/tracks",
        json={"stable_ids": reorder},
        headers={"If-Match": first.headers["ETag"]},
    )
    assert second.status_code == 200, second.text

    undone = client.post("/api/v1/playlist-history/undo")
    assert undone.status_code == 200, undone.text
    assert undone.json()["current"]["items"] == ["t-001", "t-002", "t-003", "t-004"]
    assert client.get(f"/api/v1/playlists/{pid}").json()["items"] == [
        "t-001",
        "t-002",
        "t-003",
        "t-004",
    ]

    redone = client.post("/api/v1/playlist-history/redo")
    assert redone.status_code == 200, redone.text
    assert redone.json()["current"]["items"] == reorder
    assert redone.headers.get("ETag") == redone.json()["etag"]
    live = client.get(f"/api/v1/playlists/{pid}")
    assert live.json()["items"] == reorder
    assert live.headers.get("ETag") == redone.headers.get("ETag")
    assert client.get("/api/v1/playlist-history").json()["can_redo"] is False
