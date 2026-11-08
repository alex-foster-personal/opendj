"""HTTP CRUD preserves the virtual edit contract and undo boundary.

[if] edit HTTP writes bypass validation or undo [then] the API contract fails, [else stop].
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server.deps import get_read_state, get_write_state
from apps.webui.server.routes import library_edits, library_org

pytestmark = pytest.mark.requirement("LIBEDIT-9179")

HEADERS = {"X-Undo-Session": "route-test"}


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path)
    (tmp_path / "machine-id").write_text("a" * 32, encoding="utf-8")
    writer = StateWriter(conn, FakeEventBus(), actor="seed")
    writer.upsert_track(
        stable_id="source-1",
        stable_id_tier="inferred",
        title="Source",
        artists=["Artist"],
        album=None,
        isrc=None,
        duration_ms=10_000,
        file_path=str(tmp_path / "missing.wav"),
    )
    writer.close()
    conn.close()
    app = FastAPI()
    app.state.state_db_path = path
    app.include_router(library_org.router)
    app.include_router(library_edits.router)
    app.dependency_overrides[get_read_state] = lambda: None
    app.dependency_overrides[get_write_state] = lambda: None
    yield TestClient(app)


def test_writes_require_an_undo_session(client: TestClient) -> None:
    response = client.post(
        "/edits",
        json={"source_track_id": "source-1", "in_ms": 0, "out_ms": 1_000},
    )
    assert response.status_code == 422


def test_create_patch_delete_and_undo_round_trip(client: TestClient) -> None:
    made = client.post(
        "/edits",
        headers=HEADERS,
        json={
            "source_track_id": "source-1",
            "label": "Intro",
            "in_ms": 1_000,
            "out_ms": 8_000,
            "fx": {"filter": 0.4},
        },
    )
    assert made.status_code == 201, made.text
    edit_id = made.json()["row"]["id"]
    assert made.json()["undo_id"]
    assert client.get("/edits", params={"source_track_id": "source-1"}).json()["rows"][0]["fx"] == {"filter": 0.4}

    patched = client.patch(
        f"/edits/{edit_id}",
        headers=HEADERS,
        json={"label": "Short intro", "out_ms": 6_000},
    )
    assert patched.status_code == 200
    assert patched.json()["row"]["label"] == "Short intro"

    deleted = client.delete(f"/edits/{edit_id}", headers=HEADERS)
    assert deleted.status_code == 200
    assert client.get("/edits").json()["rows"] == []
    undone = client.post("/undo", headers=HEADERS)
    assert undone.status_code == 200
    assert undone.json()["op"] == "edit.delete"
    assert client.get("/edits").json()["rows"][0]["id"] == edit_id

    app = cast(FastAPI, client.app)
    conn = state_db.open_ro(Path(app.state.state_db_path))
    try:
        assert conn.execute("SELECT count(*) FROM tracks WHERE stable_id = 'source-1'").fetchone()[0] == 1
    finally:
        conn.close()


@pytest.mark.parametrize(("in_ms", "out_ms"), [(1_000, 1_000), (2_000, 1_000)])
def test_out_not_after_in_is_a_typed_422(client: TestClient, in_ms: int, out_ms: int) -> None:
    refused = client.post(
        "/edits",
        headers=HEADERS,
        json={
            "source_track_id": "source-1",
            "in_ms": in_ms,
            "out_ms": out_ms,
        },
    )
    assert refused.status_code == 422
    assert refused.json()["detail"]["error"] == "VALIDATION"
