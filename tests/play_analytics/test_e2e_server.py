"""Pins that the play-analytics fixture host serves the app-shell routes."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.play_analytics.e2e_server import create_app


def test_fixture_host_serves_shell_routes_from_throwaway_dir(tmp_path: Path) -> None:
    app = create_app(tmp_path / "events.db")
    with TestClient(app) as client:
        tracks = client.get("/api/v1/tracks")
        playlists = client.get("/api/v1/playlists")
        client_errors = client.get("/api/v1/client-errors")
        flags = client.get("/api/v1/flags")
        app_update = client.get("/api/v1/app-update/status")
        analytics = client.get("/api/play-analytics")

    assert tracks.status_code == 200
    assert playlists.status_code == 200
    assert client_errors.status_code == 200
    assert flags.status_code == 200
    assert app_update.status_code == 200
    assert analytics.status_code == 200
    summary = analytics.json()["summary"]
    assert summary["sessions"] == 2
    assert summary["plays"] == 4

    log_dir = Path(app.state.client_error_log_dir)
    assert str(log_dir).startswith(str(tmp_path))

    paths = app.openapi()["paths"]
    assert "/api/v1/tracks" in paths
    assert "/api/v1/playlists" in paths
    assert "/api/v1/client-errors" in paths
    assert "/api/v1/flags" in paths
    assert "/api/v1/app-update/status" in paths
