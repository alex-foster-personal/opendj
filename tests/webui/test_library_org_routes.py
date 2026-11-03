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
    duplicate = client.post(
        "/tag-types", json={"id": "x", "label": "X", "singular": "X"}, headers=HEADERS
    )
    assert (duplicate.status_code, duplicate.json()["detail"]["error"]) == (409, "CONFLICT")


def test_unknown_field_is_refused(client: TestClient) -> None:
    assert client.post(
        "/sections",
        json={"owner_kind": "set", "owner_id": "1", "before_position": 0, "nope": 1},
        headers=HEADERS,
    ).status_code == 422


def test_section_round_trip(client: TestClient) -> None:
    made = client.post(
        "/sections", json={"owner_kind": "set", "owner_id": "1", "before_position": 2},
        headers=HEADERS,
    ).json()
    listed = client.get("/sections", params={"owner_kind": "set", "owner_id": "1"})
    assert [r["id"] for r in listed.json()["rows"]] == [made["row"]["id"]]
    assert client.delete(f"/sections/{made['row']['id']}", headers=HEADERS).status_code == 200


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
