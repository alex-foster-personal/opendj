"""HTTP tests for playlist sets (SET-05)."""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.routes import playlist_write
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = pytest.mark.requirement("SET-05")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path)
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


def _create_playlist(client: TestClient, name: str) -> str:
    r = client.post("/api/v1/playlists", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["playlist_id"]


def _put_tracks(client: TestClient, playlist_id: str, stable_ids: list[str]) -> None:
    detail = client.get(f"/api/v1/playlists/{playlist_id}").json()
    etag = detail["updated_at"]
    r = client.put(
        f"/api/v1/playlists/{playlist_id}/tracks",
        json={"stable_ids": stable_ids},
        headers={"If-Match": etag},
    )
    assert r.status_code == 200, r.text


def test_create_two_sets_and_list(client: TestClient, db_path: Path) -> None:
    """[if] a playlist holds several sets [then] list returns names and play_count."""
    playlist_id = _create_playlist(client, "Party")
    create_a = client.post(
        f"/api/v1/playlists/{playlist_id}/sets",
        json={"name": "AtlantisParty"},
    )
    create_b = client.post(
        f"/api/v1/playlists/{playlist_id}/sets",
        json={"name": "Warmup"},
    )
    assert create_a.status_code == 201, create_a.text
    assert create_b.status_code == 201, create_b.text
    listed = client.get(f"/api/v1/playlists/{playlist_id}/sets")
    assert listed.status_code == 200, listed.text
    body = listed.json()["sets"]
    assert {s["name"] for s in body} == {"AtlantisParty", "Warmup"}
    assert all(s["play_count"] == 0 for s in body)


def test_performance_run_increments_without_playlist_mutation(
    client: TestClient, db_path: Path
) -> None:
    """[if] a set is performed [then] play_count increments and playlist unchanged."""
    playlist_id = _create_playlist(client, "Gig")
    before = client.get(f"/api/v1/playlists/{playlist_id}")
    assert before.status_code == 200, before.text
    before_body = before.json()
    before_etag = before.headers.get("ETag")
    created = client.post(
        f"/api/v1/playlists/{playlist_id}/sets",
        json={"name": "Main"},
    )
    assert created.status_code == 201, created.text
    set_id = created.json()["id"]
    run = client.post(
        f"/api/v1/playlists/{playlist_id}/sets/{set_id}/runs",
        json={"kind": "performance"},
    )
    assert run.status_code == 201, run.text
    assert run.json()["play_count"] == 1
    after = client.get(f"/api/v1/playlists/{playlist_id}")
    assert after.status_code == 200, after.text
    assert after.json()["items"] == before_body["items"]
    assert after.headers.get("ETag") == before_etag


def test_practice_run_does_not_increment(client: TestClient) -> None:
    """[if] the user is practicing [then] play_count stays 0 with a practice run."""
    playlist_id = _create_playlist(client, "Practice")
    created = client.post(
        f"/api/v1/playlists/{playlist_id}/sets",
        json={"name": "Rehearsal"},
    )
    assert created.status_code == 201, created.text
    set_id = created.json()["id"]
    run = client.post(
        f"/api/v1/playlists/{playlist_id}/sets/{set_id}/runs",
        json={"kind": "practice"},
    )
    assert run.status_code == 201, run.text
    assert run.json()["play_count"] == 0
    detail = client.get(f"/api/v1/playlists/{playlist_id}/sets/{set_id}")
    assert detail.status_code == 200, detail.text
    runs = detail.json()["runs"]
    assert len(runs) == 1
    assert runs[0]["kind"] == "practice"


def test_openapi_operation_ids() -> None:
    paths = create_app(mount_frontend=False).openapi()["paths"]
    list_path = paths["/api/v1/playlists/{playlist_id}/sets"]
    assert list_path["get"]["operationId"] == "list_playlist_sets"
    assert list_path["post"]["operationId"] == "create_playlist_set"
    run_path = paths["/api/v1/playlists/{playlist_id}/sets/{set_id}/runs"]
    assert run_path["post"]["operationId"] == "create_playlist_set_run"
