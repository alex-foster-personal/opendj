"""Playlist membership routes in the live OpenAPI schema (one fetch per module)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server.app import create_app
from apps.webui.server.routes import playlist_write
from apps.webui.server.sqlite_backend import SqliteBackend

_ROUTE_CASES: tuple[pytest.ParameterSet | tuple[str, str | None], ...] = (
    pytest.param(
        "/api/v1/playlists/{playlist_id}/items:add",
        "post",
        marks=pytest.mark.requirement("LIBM-20"),
    ),
    pytest.param(
        "/api/v1/playlists/{playlist_id}/items:move",
        "post",
        marks=pytest.mark.requirement("LIBM-22"),
    ),
    pytest.param(
        "/api/v1/playlists/{playlist_id}/items/{item_id}",
        "delete",
        marks=pytest.mark.requirement("LIBM-21"),
    ),
    pytest.param(
        "/api/v1/playlists/{playlist_id}/items:remove",
        "post",
        marks=pytest.mark.requirement("LIBM-21"),
    ),
    ("/api/v1/playlists/{playlist_id}/tracks", None),
)


@pytest.fixture(scope="module")
def openapi_paths(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    db_path = tmp_path_factory.mktemp("playlist-openapi") / "state.db"
    conn = state_db.open_rw(db_path)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="test-seed")
    try:
        writer.upsert_track(
            stable_id="t-001",
            stable_id_tier="inferred",
            title="Track 1",
            artists=["Artist"],
            album=None,
            isrc=None,
            duration_ms=180_000,
            file_path=None,
        )
    finally:
        writer.close()
        conn.close()
    app = create_app(
        backend=SqliteBackend(db_path),
        state_db_path=str(db_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
        mount_frontend=False,
    )
    with TestClient(app) as client:
        spec = client.get("/openapi.json")
        assert spec.status_code == 200
        paths: dict[str, object] = spec.json()["paths"]
    playlist_write.close_store(app)
    return paths


@pytest.mark.parametrize(
    ("path", "method"),
    _ROUTE_CASES,
    ids=["items-add", "items-move", "items-delete", "items-remove", "tracks"],
)
def test_openapi_documents_playlist_route(
    openapi_paths: dict[str, object],
    path: str,
    method: str | None,
) -> None:
    """[if] a playlist route is served [then] openapi.json documents it, [else stop]."""
    assert path in openapi_paths
    if method is not None:
        assert method in openapi_paths[path]
