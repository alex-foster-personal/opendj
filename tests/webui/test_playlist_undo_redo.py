"""Route-level regression for playlist undo/redo empty-history refusals (issue #12222).

Regression one-liners:
  * if empty undo is not 409 nothing_to_undo with unchanged playlists and no hub publish then broken
  * if redo at stack end is not 409 nothing_to_redo with unchanged row/history and no hub publish then broken
  * if success after refusal does not match acknowledged command_id and single library.changed then broken
  * if conflict undo is not 409 without hub publish then broken
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.shared import events
from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server.app import create_app
from apps.webui.server.playlist_store import PlaylistStore
from apps.webui.server.routes import playlist_write
from apps.webui.server.sqlite_backend import SqliteBackend

TRACK_IDS: list[str] = ["t-001", "t-002", "t-003", "t-004"]


class _Hub:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        self.events.append((topic, payload))


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


def _assert_current_matches_live(
    client: TestClient,
    playlist_id: str,
    current: dict[str, Any],
    response_etag: str | None,
) -> None:
    live_resp = client.get(f"/api/v1/playlists/{playlist_id}")
    assert live_resp.status_code == 200, live_resp.text
    live = live_resp.json()
    assert current["playlist_id"] == live["playlist_id"]
    assert current["name"] == live["name"]
    assert current["items"] == live["items"]
    assert response_etag == live_resp.headers.get("ETag")


def test_undo_empty_history_refuses_without_mutation(client: TestClient) -> None:
    playlists_before = client.get("/api/v1/playlists")
    assert playlists_before.status_code == 200, playlists_before.text
    history_before = client.get("/api/v1/playlist-history")
    assert history_before.status_code == 200, history_before.text

    hub = _Hub()
    events.set_hub(hub)
    try:
        undo = client.post("/api/v1/playlist-history/undo")
    finally:
        events.set_hub(None)

    assert undo.status_code == 409, undo.text
    assert undo.json() == {"error": "nothing_to_undo"}
    assert undo.headers.get("ETag") is None

    playlists_after = client.get("/api/v1/playlists")
    assert playlists_after.status_code == 200
    assert playlists_after.content == playlists_before.content

    history_after = client.get("/api/v1/playlist-history")
    assert history_after.status_code == 200
    assert history_after.json() == history_before.json()

    assert hub.events == []


def test_redo_empty_history_refuses_without_mutation(client: TestClient) -> None:
    body, etag = _create(client, "Only")
    pid = body["playlist_id"]

    detail_before = client.get(f"/api/v1/playlists/{pid}")
    assert detail_before.status_code == 200, detail_before.text
    history_before = client.get("/api/v1/playlist-history")
    assert history_before.status_code == 200, history_before.text
    assert history_before.json()["can_redo"] is False

    hub = _Hub()
    events.set_hub(hub)
    try:
        redo = client.post("/api/v1/playlist-history/redo")
    finally:
        events.set_hub(None)

    assert redo.status_code == 409, redo.text
    assert redo.json() == {"error": "nothing_to_redo"}
    assert redo.headers.get("ETag") is None

    detail_after = client.get(f"/api/v1/playlists/{pid}")
    assert detail_after.status_code == 200
    assert detail_after.json() == detail_before.json()
    assert detail_after.headers.get("ETag") == etag

    history_after = client.get("/api/v1/playlist-history")
    assert history_after.json() == history_before.json()

    assert hub.events == []


def test_undo_redo_after_refusal_acknowledged_command_and_single_publish(
    client: TestClient,
) -> None:
    hub = _Hub()
    events.set_hub(hub)
    try:
        refusal = client.post("/api/v1/playlist-history/undo")
    finally:
        events.set_hub(None)
    assert refusal.status_code == 409
    assert hub.events == []

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
    assert renamed.status_code == 200, renamed.text

    hist = client.get("/api/v1/playlist-history").json()
    cursor = hist["cursor"]
    assert cursor > 0
    expected_undo_cmd = hist["entries"][cursor - 1]["command_id"]

    hub = _Hub()
    events.set_hub(hub)
    try:
        undone = client.post("/api/v1/playlist-history/undo")
    finally:
        events.set_hub(None)

    assert undone.status_code == 200, undone.text
    undo_body = undone.json()
    assert undo_body["command_id"] == expected_undo_cmd
    assert undo_body["action"] == "undo"
    assert undo_body["current"] is not None
    _assert_current_matches_live(
        client,
        pid,
        undo_body["current"],
        undone.headers.get("ETag"),
    )

    fresh_hist = client.get("/api/v1/playlist-history").json()
    assert undo_body["can_undo"] == fresh_hist["can_undo"]
    assert undo_body["can_redo"] == fresh_hist["can_redo"]

    playlist_events = [e for e in hub.events if e[0] == "library.changed"]
    assert playlist_events == [("library.changed", {"kind": "playlists", "ids": [pid]})]

    hist = client.get("/api/v1/playlist-history").json()
    expected_redo_cmd = hist["entries"][hist["cursor"]]["command_id"]

    hub = _Hub()
    events.set_hub(hub)
    try:
        redone = client.post("/api/v1/playlist-history/redo")
    finally:
        events.set_hub(None)

    assert redone.status_code == 200, redone.text
    redo_body = redone.json()
    assert redo_body["command_id"] == expected_redo_cmd
    assert redo_body["action"] == "redo"
    assert redo_body["current"] is not None
    _assert_current_matches_live(
        client,
        pid,
        redo_body["current"],
        redone.headers.get("ETag"),
    )

    playlist_events = [e for e in hub.events if e[0] == "library.changed"]
    assert playlist_events == [("library.changed", {"kind": "playlists", "ids": [pid]})]


def test_undo_conflict_refuses_without_publish(client: TestClient, db_path: Path) -> None:
    body, etag = _create(client, "Warmup")
    pid = body["playlist_id"]
    put = client.put(
        f"/api/v1/playlists/{pid}/tracks",
        json={"stable_ids": ["t-001"]},
        headers={"If-Match": etag},
    )
    assert put.status_code == 200, put.text
    history_before = client.get("/api/v1/playlist-history").json()

    with PlaylistStore(db_path, bus=FakeEventBus()) as sneaky:
        sneaky.replace_memberships(
            pid,
            ["t-002"],
            expected_etag=put.headers["ETag"],
            record_edit=False,
        )

    hub = _Hub()
    events.set_hub(hub)
    try:
        conflict = client.post("/api/v1/playlist-history/undo")
    finally:
        events.set_hub(None)

    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"] == "conflict"
    assert client.get("/api/v1/playlist-history").json() == history_before
    assert hub.events == []
