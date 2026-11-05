"""HTTP contract for stacks, suggestions and classical metadata (R-API-1, R-API-2)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state import schema as state_schema
from apps.webui.server.deps import get_read_state, get_write_state
from apps.webui.server.routes import library_org, library_works

HEADERS = {"X-Undo-Session": "s"}
T0 = "2026-10-30T00:00:00Z"


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path)
    state_schema.apply_migrations(conn)
    for stable_id, title in (("a", "Lacrimosa (Remix)"), ("b", "Lacrimosa (Edit)"), ("c", "Other")):
        conn.execute(
            "INSERT INTO tracks(stable_id, stable_id_tier, title, artists_json, file_path,"
            " created_at, updated_at) VALUES (?, 'inferred', ?, '[\"X\"]', ?, ?, ?)",
            (stable_id, title, str(tmp_path / f"{stable_id}.mp3"), T0, T0),
        )
    conn.commit()
    conn.close()
    (tmp_path / "machine-id").write_text("a" * 32)
    app = FastAPI()
    app.state.state_db_path = path
    app.include_router(library_org.router)
    app.include_router(library_works.router)
    app.dependency_overrides[get_read_state] = lambda: None
    app.dependency_overrides[get_write_state] = lambda: None
    yield TestClient(app)


def _link(client: TestClient, *ids: str, **extra: object) -> dict:
    body = {"member_ids": list(ids), **extra}
    response = client.post("/works:link", json=body, headers=HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


def test_link_then_stack_then_undo(client: TestClient) -> None:
    linked = _link(client, "a", "b", title="Lacrimosa")
    assert linked["undo_id"]
    stack = client.get("/works/stack", params={"member": "a"}).json()
    assert {m["member_id"] for m in stack["members"]} == {"a", "b"}
    assert client.post("/undo", headers=HEADERS).json()["op"] == "work.link_many"
    assert client.get("/works/stack", params={"member": "a"}).json()["members"] == []


def test_every_write_needs_an_undo_session(client: TestClient) -> None:
    assert client.post("/works:link", json={"member_ids": ["a", "b"]}).status_code == 422
    assert client.patch("/tracks/a/classical", json={"label": "DG"}).status_code == 422


def test_conflict_carries_what_a_client_needs_to_offer_a_merge(client: TestClient) -> None:
    work_id = client.post("/works", json={"kind": "song", "title": "Taken"}, headers=HEADERS).json()["row"]["id"]
    other = client.post("/works", json={"kind": "song", "title": "Free"}, headers=HEADERS).json()
    refused = client.patch(f"/works/{other['row']['id']}", json={"title": "taken"}, headers=HEADERS)
    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert (detail["error"], detail["existing_work_id"]) == ("CONFLICT", work_id)


def test_merge_move_and_member_update_routes(client: TestClient) -> None:
    work_id = _link(client, "a", "b", title="Lacrimosa")["row"]["id"]
    assert client.post(f"/works/{work_id}/members:move", json={"member_ids": ["b", "a"]}, headers=HEADERS).json()[
        "undo_id"
    ]
    assert (
        client.patch(f"/works/{work_id}/members/track/a", json={"role": "cover"}, headers=HEADERS).json()["row"]["role"]
        == "cover"
    )
    other = client.post("/works", json={"kind": "song", "title": "Z"}, headers=HEADERS).json()
    assert client.post(f"/works/{other['row']['id']}:merge", json={"into": work_id}, headers=HEADERS).status_code == 200


def test_a_bad_role_is_refused(client: TestClient) -> None:
    response = client.post(
        "/works:link",
        json={"member_ids": ["a", "b"], "title": "X", "role": "nope"},
        headers=HEADERS,
    )
    assert response.status_code == 422


def test_suggestions_and_flags(client: TestClient) -> None:
    rows = client.get("/works/suggest", params={"member": "a"}).json()["rows"]
    assert [r["member_id"] for r in rows] == ["b"]
    flags = client.post("/works/suggestion-flags", json={"member_ids": ["a", "c"]})
    assert flags.json()["member_ids"] == ["a"]
    assert (
        client.get("/works/link-candidates", params={"member": "a", "q": "oth"}).json()["rows"][0]["member_id"] == "c"
    )


def test_classical_routes_and_overlay(client: TestClient) -> None:
    written = client.patch("/tracks/a/classical", json={"label": "DG", "recording_year": 1962}, headers=HEADERS)
    assert written.json()["undo_id"]
    assert client.get("/tracks/a/classical").json()["values"]["label"] == "DG"
    assert client.get("/works/overlay").json()["classical"]["a"]["recording_year"] == 1962
    assert client.patch("/tracks/a/classical", json={"recording_year": "x"}, headers=HEADERS).status_code == 422


def test_file_tags_fail_loudly_for_a_missing_track(client: TestClient) -> None:
    assert client.get("/tracks/ghost/classical/file-tags").status_code == 404
