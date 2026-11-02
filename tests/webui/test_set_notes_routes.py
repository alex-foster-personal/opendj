"""HTTP tests for set notes (SETNOTE-6340c), mounted by the web UI from apps.set_notes.http.

[if] a set note is read or written over HTTP [then] 404/rev/409/422 hold, [else stop].
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.routes import playlist_write
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = pytest.mark.requirement("SETNOTE-6340c")


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    db_path = tmp_path / "state.db"
    state_db.open_rw(db_path).close()
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


def _put(client: TestClient, scope: str, base_rev: int, body: str = "# Set"):
    return client.put(
        f"/api/v1/set-notes/{scope}",
        json={"body_md": body, "x": 10, "y": 20, "w": 300, "h": 200,
              "visible": True, "base_rev": base_rev},
    )


def test_get_missing_is_404(client: TestClient) -> None:
    """[if] no note was saved for a scope [then] GET is 404, [else stop]."""
    r = client.get("/api/v1/set-notes/scratch")
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == {"error": "not_found"}


def test_create_then_get(client: TestClient) -> None:
    """[if] PUT base_rev 0 creates a note [then] GET returns it at rev 1, [else stop]."""
    created = _put(client, "playlist:abc", 0, "hello [[track:T1|A - B]]")
    assert created.status_code == 200, created.text
    assert created.json()["rev"] == 1
    got = client.get("/api/v1/set-notes/playlist:abc")
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["body_md"] == "hello [[track:T1|A - B]]"
    assert (body["x"], body["y"], body["w"], body["h"]) == (10, 20, 300, 200)
    assert body["visible"] is True
    assert body["updated_at"].endswith("+00:00")


def test_stale_rev_is_409(client: TestClient) -> None:
    """[if] PUT carries a stale base_rev [then] 409 names the current rev, [else stop]."""
    assert _put(client, "scratch", 0).status_code == 200
    assert _put(client, "scratch", 1, "v2").json()["rev"] == 2
    stale = _put(client, "scratch", 1, "lost")
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"] == {"error": "stale_rev", "current_rev": 2}
    recreate = _put(client, "scratch", 0, "dup")
    assert recreate.status_code == 409, recreate.text
    assert client.get("/api/v1/set-notes/scratch").json()["body_md"] == "v2"


@pytest.mark.parametrize("scope", ["nope", "playlist_set:abc", "playlist:"])
def test_invalid_scope_is_422(client: TestClient, scope: str) -> None:
    """[if] the scope is not scratch/playlist/playlist_set [then] 422, [else stop]."""
    assert client.get(f"/api/v1/set-notes/{scope}").status_code == 422
    assert _put(client, scope, 0).status_code == 422
