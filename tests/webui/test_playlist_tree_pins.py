"""Pinned playlists as tree shortcuts (LIBM-87 pin part, issue #12320).

[if] a user pins a playlist [then] GET /playlist-tree/pins lists it, apart from the tree order, [else stop].
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state.events import EventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server import playlist_tree_order as tree_order
from apps.webui.server import playlist_tree_pins as tree_pins
from apps.webui.server.app import create_app
from apps.webui.server.routes import playlist_write
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = pytest.mark.requirement("LIBM-13988")

PINS = "/api/v1/playlist-tree/pins"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state" / "state.db"
    path.parent.mkdir(parents=True)
    conn = state_db.open_rw(path)
    bus = EventBus()
    try:
        StateWriter(conn, bus=bus, actor="test-seed").close()
    finally:
        bus.close()
        conn.close()
    return path


def _app_client(db_path: Path) -> Iterator[TestClient]:
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


@pytest.fixture
def client(db_path: Path) -> Iterator[TestClient]:
    yield from _app_client(db_path)


def _create(client: TestClient, name: str) -> str:
    r = client.post("/api/v1/playlists", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["playlist_id"]


def _tree(client: TestClient) -> list[tuple[str, int | None, int]]:
    r = client.get("/api/v1/playlists", params={"availability": "skip"})
    assert r.status_code == 200, r.text
    return [(row["playlist_id"], row["seq"], row["track_count"]) for row in r.json()]


def test_pinning_an_ordinary_playlist_lists_it_and_keeps_the_tree(client: TestClient) -> None:
    """[if] an ordinary playlist is pinned [then] pins list it and tree order and counts stay, [else stop]."""
    first, second = _create(client, "Warm Up"), _create(client, "Peak")
    before = _tree(client)
    r = client.put(f"{PINS}/{second}")
    assert r.status_code == 200, r.text
    assert r.json() == {"pins": [second]}
    assert client.put(f"{PINS}/{first}").json() == {"pins": [second, first]}, "pins keep pin order"
    assert client.put(f"{PINS}/{second}").json() == {"pins": [second, first]}, "pinning twice is a no-op"
    assert _tree(client) == before


def test_unpinning_a_pinned_playlist_drops_only_the_pin(client: TestClient) -> None:
    """[if] a pinned playlist is unpinned [then] the pin goes and the playlist stays, [else stop]."""
    pid = _create(client, "Closing")
    client.put(f"{PINS}/{pid}")
    r = client.delete(f"{PINS}/{pid}")
    assert r.status_code == 200, r.text
    assert r.json() == {"pins": []}
    assert client.get(PINS).json() == {"pins": []}
    assert pid in {row[0] for row in _tree(client)}
    assert client.delete(f"{PINS}/{pid}").json() == {"pins": []}, "unpinning an unpinned playlist is a no-op"


def test_pins_survive_a_fresh_tree_read_apart_from_the_order(client: TestClient, db_path: Path) -> None:
    """[if] the tree reloads after pinning [then] a new app reads the same pins from their own file, [else stop]."""
    pid = _create(client, "Gig Bag")
    client.put(f"{PINS}/{pid}")
    data_dir = db_path.parent.parent
    assert tree_pins.load_pins(tree_pins.path_for(data_dir)) == [pid]
    assert not tree_order.path_for(data_dir).exists(), "a pin never writes the tree order"
    fresh = _app_client(db_path)
    assert next(fresh).get(PINS).json() == {"pins": [pid]}
    fresh.close()


def test_pinning_a_missing_playlist_is_refused_and_not_written(client: TestClient, db_path: Path) -> None:
    """[if] a missing playlist id is pinned [then] 404 playlist_not_found and nothing is stored, [else stop]."""
    r = client.put(f"{PINS}/no-such-playlist")
    assert r.status_code == 404, r.text
    assert r.json()["detail"]["error"] == "playlist_not_found"
    assert not tree_pins.path_for(db_path.parent.parent).exists()


def test_deleted_playlist_drops_out_of_the_pins(client: TestClient) -> None:
    """[if] a pinned playlist is deleted [then] GET pins leaves it out, [else stop]."""
    keep, gone = _create(client, "Keep"), _create(client, "Gone")
    client.put(f"{PINS}/{keep}")
    client.put(f"{PINS}/{gone}")
    etag = client.get(f"/api/v1/playlists/{gone}").headers["ETag"]
    assert client.delete(f"/api/v1/playlists/{gone}", headers={"If-Match": etag}).status_code == 204
    assert client.get(PINS).json() == {"pins": [keep]}


def test_corrupt_pins_file_fails_loud(client: TestClient, db_path: Path) -> None:
    """[if] the pins file is not valid JSON [then] GET pins answers a structured 500, [else stop]."""
    path = tree_pins.path_for(db_path.parent.parent)
    path.write_text("{not json", encoding="utf-8")
    r = client.get(PINS)
    assert r.status_code == 500, r.text
    assert r.json()["detail"]["error"] == "playlist_tree_pins_unreadable"
