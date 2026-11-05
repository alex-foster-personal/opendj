"""HTTP contract for the library organization foundation (R-API-2)."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state import schema as state_schema
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server.deps import get_read_state, get_write_state
from apps.webui.server.routes import library_org

HEADERS = {"X-Undo-Session": "s"}


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path)
    state_schema.apply_migrations(conn)
    conn.close()
    (tmp_path / "machine-id").write_text("a" * 32)
    app = FastAPI()
    app.state.state_db_path = path
    app.include_router(library_org.router)
    app.dependency_overrides[get_read_state] = lambda: None
    app.dependency_overrides[get_write_state] = lambda: None
    yield TestClient(app)


def test_a_write_needs_an_undo_session(client: TestClient) -> None:
    assert client.post("/works", json={"kind": "song", "title": "A"}).status_code == 422


def test_write_returns_row_and_undo_id_then_undo_reverts(client: TestClient) -> None:
    created = client.post("/works", json={"kind": "song", "title": "A"}, headers=HEADERS)
    assert created.status_code == 201
    assert created.json()["undo_id"]
    assert client.post("/undo", headers=HEADERS).json()["op"] == "work.create"
    assert client.get("/works").json()["rows"] == []


def test_typed_errors(client: TestClient) -> None:
    missing = client.patch("/works/nope", json={"title": "B"}, headers=HEADERS)
    assert (missing.status_code, missing.json()["detail"]["error"]) == (404, "NOT_FOUND")
    client.post("/tag-types", json={"id": "x", "label": "X", "singular": "X"}, headers=HEADERS)
    duplicate = client.post("/tag-types", json={"id": "x", "label": "X", "singular": "X"}, headers=HEADERS)
    assert (duplicate.status_code, duplicate.json()["detail"]["error"]) == (409, "CONFLICT")


def test_unknown_field_is_refused(client: TestClient) -> None:
    assert (
        client.post(
            "/sections",
            json={"owner_kind": "set", "owner_id": "1", "before_position": 0, "nope": 1},
            headers=HEADERS,
        ).status_code
        == 422
    )


def test_section_round_trip(client: TestClient) -> None:
    made = client.post(
        "/sections",
        json={"owner_kind": "set", "owner_id": "1", "before_position": 2},
        headers=HEADERS,
    ).json()
    listed = client.get("/sections", params={"owner_kind": "set", "owner_id": "1"})
    assert [r["id"] for r in listed.json()["rows"]] == [made["row"]["id"]]
    assert client.delete(f"/sections/{made['row']['id']}", headers=HEADERS).status_code == 200


def test_sections_list_top_to_bottom_and_entries_move_needs_a_session(client: TestClient) -> None:
    conn = state_db.open_rw(cast(FastAPI, client.app).state.state_db_path)
    conn.execute(
        "INSERT INTO playlists(playlist_id, name, vendor, vendor_pl_id, created_at, updated_at) "
        "VALUES (\x27p\x27, \x27P\x27, \x27local\x27, \x271\x27, \x27t\x27, \x27t\x27)"
    )
    conn.commit()
    conn.close()
    body = {"owner_kind": "playlist", "owner_id": "p", "before_position": 3}
    low = client.post("/sections", json=body, headers=HEADERS).json()["row"]["id"]
    high = client.post("/sections", json={**body, "before_position": 0}, headers=HEADERS)
    rows = client.get("/sections", params={"owner_kind": "playlist", "owner_id": "p"}).json()["rows"]
    assert [r["id"] for r in rows] == [high.json()["row"]["id"], low]
    assert client.post("/playlists/p/entries:move", json={}).status_code == 422
    empty = client.post("/playlists/p/entries:move", json={}, headers=HEADERS)
    assert (empty.status_code, empty.json()["detail"]["error"]) == (422, "VALIDATION")
    two = client.post(
        "/playlists/p/entries:move",
        json={"section_ids": [low], "before_item_id": "a", "before_section_id": "b"},
        headers=HEADERS,
    )
    assert (two.status_code, two.json()["detail"]["error"]) == (422, "VALIDATION")
    moved = client.post("/playlists/p/entries:move", json={"section_ids": [low]}, headers=HEADERS)
    assert moved.status_code == 200
    assert client.post("/undo", headers=HEADERS).json()["op"] == "entries.move"


def test_a_track_move_stays_on_the_undo_button(client: TestClient) -> None:
    """[if] a track moves by entries:move [then] playlist history undoes it, [else stop]."""
    app = cast(FastAPI, client.app)
    conn = state_db.open_rw(app.state.state_db_path)
    writer = StateWriter(conn, FakeEventBus(), actor="seed")
    writer.insert_playlist(playlist_id="m", name="M", vendor="local", vendor_pl_id="m")
    for stable_id in "abc":
        writer.upsert_track(
            stable_id=stable_id,
            stable_id_tier="isrc",
            title=stable_id,
            artists=["x"],
            album=None,
            isrc=None,
            duration_ms=None,
            file_path=None,
        )
    writer.set_playlist_memberships("m", list("abc"))
    items = dict(conn.execute("SELECT stable_id, item_id FROM playlist_memberships WHERE playlist_id = 'm'").fetchall())
    conn.close()
    moved = client.post(
        "/playlists/m/entries:move",
        json={"item_ids": [items["c"]], "before_item_id": items["a"]},
        headers=HEADERS,
    )
    assert moved.status_code == 200
    store = app.state.playlist_store
    assert store.get_playlist_row("m").items == ["c", "a", "b"]
    store.undo()
    assert store.get_playlist_row("m").items == ["a", "b", "c"]
    store.close()


def test_a_request_connection_works_on_another_thread(tmp_path: Path) -> None:
    """FastAPI opens it on one threadpool thread and uses it on another."""
    path = tmp_path / "state.db"
    state_db.open_rw(path).close()
    state = SimpleNamespace(state_db_path=path)
    request = cast(Any, SimpleNamespace(app=SimpleNamespace(state=state)))
    reader = library_org._reader(request, cast(Any, None))
    conn = next(reader)
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(lambda: conn.execute("SELECT 1").fetchone()[0]).result() == 1
    assert next(reader, None) is None
    session = library_org._session(request, cast(Any, None), "s")
    writer = next(session).writer
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(lambda: writer._conn.execute("SELECT 1").fetchone()[0]).result() == 1
    assert next(session, None) is None
