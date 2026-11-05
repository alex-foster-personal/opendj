"""LIBUX-8626 history popover backend: named delete entries and POST /jump.

  - [if] a history jump lands on an entry [then] the playlist matches that point, [else stop].

Regression one-liners:
  * if a Backspace-style DELETE of one playlist row is not the newest history
    entry, named after the track, with its stable_id, then broken
  * if POST /playlist-history/jump on an applied entry does not undo it and
    every newer entry (cursor lands on its index) then broken
  * if POST /playlist-history/jump on a pending entry does not redo it and
    every older pending entry then broken
  * if POST /playlist-history/jump with an unknown command_id is not 404 then broken
  * if redo of a row removal after a full membership replace empties the
    playlist then broken
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
from apps.webui.server.routes import playlist_write
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = pytest.mark.requirement("LIBUX-8626")

TRACK_IDS: list[str] = ["t-001", "t-002", "t-003"]


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
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
    app = create_app(
        backend=SqliteBackend(path),
        state_db_path=str(path),
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
        mount_frontend=False,
    )
    with TestClient(app) as c:
        yield c
    playlist_write.close_store(app)


def _inbox_with_tracks(client: TestClient) -> str:
    created = client.post("/api/v1/playlists", json={"name": "INBOX"})
    assert created.status_code == 201, created.text
    pid = created.json()["playlist_id"]
    put = client.put(
        f"/api/v1/playlists/{pid}/tracks",
        json={"stable_ids": TRACK_IDS},
        headers={"If-Match": created.headers["ETag"]},
    )
    assert put.status_code == 200, put.text
    return pid


def _item_id_of(client: TestClient, pid: str, stable_id: str) -> str:
    page = client.get(f"/api/v1/playlists/{pid}/tracks").json()
    return next(row["item_id"] for row in page["tracks"] if row["stable_id"] == stable_id)


def _history(client: TestClient) -> dict:
    hist = client.get("/api/v1/playlist-history")
    assert hist.status_code == 200, hist.text
    return hist.json()


def test_row_delete_is_newest_entry_named_after_the_track(client: TestClient) -> None:
    pid = _inbox_with_tracks(client)
    deleted = client.delete(f"/api/v1/playlists/{pid}/items/{_item_id_of(client, pid, 't-002')}")
    assert deleted.status_code == 200, deleted.text

    hist = _history(client)
    newest = hist["entries"][hist["cursor"] - 1]
    assert newest is hist["entries"][-1]
    assert newest["label"] == "Remove 'Track 2' from 'INBOX'"
    assert newest["stable_ids"] == ["t-002"]
    assert newest["playlist_id"] == pid
    assert hist["entries"][1]["label"] == "Add 3 tracks to 'INBOX'"
    assert hist["entries"][0]["stable_ids"] == []


def test_jump_undoes_entry_and_newer_then_redoes_back(client: TestClient) -> None:
    pid = _inbox_with_tracks(client)
    client.delete(f"/api/v1/playlists/{pid}/items/{_item_id_of(client, pid, 't-001')}")
    entries = _history(client)["entries"]
    assert len(entries) == 3

    undone = client.post("/api/v1/playlist-history/jump", json={"command_id": entries[1]["command_id"]})
    assert undone.status_code == 200, undone.text
    body = undone.json()
    assert body["cursor"] == 1
    assert [step["action"] for step in body["steps"]] == ["undo", "undo"]
    assert [step["command_id"] for step in body["steps"]] == [
        entries[2]["command_id"],
        entries[1]["command_id"],
    ]
    assert client.get(f"/api/v1/playlists/{pid}").json()["items"] == []

    redone = client.post("/api/v1/playlist-history/jump", json={"command_id": entries[2]["command_id"]})
    assert redone.status_code == 200, redone.text
    assert redone.json()["cursor"] == 3
    assert [step["action"] for step in redone.json()["steps"]] == ["redo", "redo"]
    assert client.get(f"/api/v1/playlists/{pid}").json()["items"] == ["t-002", "t-003"]


def test_jump_unknown_entry_is_404(client: TestClient) -> None:
    _inbox_with_tracks(client)
    missing = client.post("/api/v1/playlist-history/jump", json={"command_id": "nope"})
    assert missing.status_code == 404, missing.text
    assert _history(client)["cursor"] == 2
