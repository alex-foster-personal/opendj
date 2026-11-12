"""HTTP contract for Spotify playlist import (issue #317)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import schema as state_schema
from apps.spotify.client import MissingCredentialsError, ReauthorizationRequired, SpotifyClient
from apps.spotify.importer import ImportOptions, ImportRun
from apps.spotify.state_writer import ensure_aux_tables
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend
from apps.webui.server.routes.spotify import router


def _make_client(tmp_path: Path) -> tuple[TestClient, sqlite3.Connection]:
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    state_schema.apply_migrations(conn)
    ensure_aux_tables(conn)
    app = FastAPI()
    app.state.backend = InMemoryBackend()
    app.state.state_db_path = db_path
    app.include_router(router, prefix="/api/v1")
    return TestClient(app), conn


@pytest.mark.requirement("SPOT-9013")
def test_production_openapi_registers_spotify_import_routes() -> None:
    """[if] import routes ship [then] production OpenAPI lists import and status, [else stop]."""
    paths = create_app(mount_frontend=False).openapi()["paths"]
    assert "/api/v1/spotify/playlists/{playlist_id}:import" in paths
    assert "/api/v1/spotify/imports/status" in paths


def test_import_confirm_mismatch_returns_422(tmp_path: Path) -> None:
    client, conn = _make_client(tmp_path)
    try:
        response = client.post(
            "/api/v1/spotify/playlists/5Ptpplbw51rSiHGWjjuVsy:import",
            json={"confirm_playlist_id": "otherplaylistid00000001"},
        )
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "SPOTIFY_IMPORT_CONFIRM_MISMATCH"
    finally:
        client.close()
        conn.close()


def test_import_invalid_playlist_id_returns_422(tmp_path: Path) -> None:
    client, conn = _make_client(tmp_path)
    try:
        response = client.post(
            "/api/v1/spotify/playlists/not-a-playlist:import",
            json={"confirm_playlist_id": "not-a-playlist"},
        )
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "SPOTIFY_PLAYLIST_ID_INVALID"
    finally:
        client.close()
        conn.close()


def test_import_status_idle_before_any_job(tmp_path: Path) -> None:
    client, conn = _make_client(tmp_path)
    try:
        response = client.get("/api/v1/spotify/imports/status")
        assert response.status_code == 200
        body = response.json()
        assert body["phase"] == "idle"
        assert body["running"] is False
    finally:
        client.close()
        conn.close()


def test_import_not_configured_when_client_id_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, conn = _make_client(tmp_path)
    monkeypatch.setattr(
        SpotifyClient,
        "from_saved",
        classmethod(lambda cls, **kwargs: (_ for _ in ()).throw(MissingCredentialsError("no id"))),
    )
    try:
        pid = "5Ptpplbw51rSiHGWjjuVsy"
        response = client.post(
            f"/api/v1/spotify/playlists/{pid}:import",
            json={"confirm_playlist_id": pid},
        )
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "SPOTIFY_NOT_CONFIGURED"
    finally:
        client.close()
        conn.close()


def test_import_reauth_required(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, conn = _make_client(tmp_path)
    monkeypatch.setattr(
        SpotifyClient,
        "from_saved",
        classmethod(lambda cls, **kwargs: (_ for _ in ()).throw(ReauthorizationRequired("sign in"))),
    )
    try:
        pid = "5Ptpplbw51rSiHGWjjuVsy"
        response = client.post(
            f"/api/v1/spotify/playlists/{pid}:import",
            json={"confirm_playlist_id": pid},
        )
        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "SPOTIFY_REAUTH_REQUIRED"
    finally:
        client.close()
        conn.close()


def test_import_extra_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, conn = _make_client(tmp_path)
    monkeypatch.setattr(
        SpotifyClient,
        "from_saved",
        classmethod(lambda cls, **kwargs: (_ for _ in ()).throw(ModuleNotFoundError("spotipy"))),
    )
    try:
        pid = "5Ptpplbw51rSiHGWjjuVsy"
        response = client.post(
            f"/api/v1/spotify/playlists/{pid}:import",
            json={"confirm_playlist_id": pid},
        )
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "SPOTIFY_EXTRA_MISSING"
    finally:
        client.close()
        conn.close()


@pytest.mark.requirement("SPOT-9013")
def test_import_builds_client_with_interactive_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] import builds a Spotify client [then] it passes interactive=False, [else stop]."""
    seen: dict[str, object] = {}
    client, conn = _make_client(tmp_path)

    def fake_from_saved(cls, **kwargs):
        seen.update(kwargs)
        return MagicMock(spec=SpotifyClient)

    monkeypatch.setattr(SpotifyClient, "from_saved", classmethod(fake_from_saved))
    monkeypatch.setattr(
        "apps.webui.server.routes.spotify.run_import",
        lambda *a, **k: ImportRun(
            playlist=SimpleNamespace(track_count=0, name="x"),
            result=SimpleNamespace(matched=[], unmatched=[]),
            reports=MagicMock(),
            write_summary=None,
            runtime_seconds=0.0,
            live=True,
        ),
    )
    try:
        pid = "5Ptpplbw51rSiHGWjjuVsy"
        response = client.post(
            f"/api/v1/spotify/playlists/{pid}:import",
            json={"confirm_playlist_id": pid},
        )
        assert response.status_code == 202
        assert seen.get("interactive") is False
    finally:
        client.close()
        conn.close()


def test_import_passes_live_options_to_run_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    client, conn = _make_client(tmp_path)
    db_path = tmp_path / "state.db"

    monkeypatch.setattr(
        SpotifyClient,
        "from_saved",
        classmethod(lambda cls, **kwargs: MagicMock(spec=SpotifyClient)),
    )

    def fake_run_import(playlist_id: str, *, client, options: ImportOptions):
        captured["playlist_id"] = playlist_id
        captured["options"] = options
        return ImportRun(
            playlist=SimpleNamespace(track_count=1, name="Invented"),
            result=SimpleNamespace(matched=[], unmatched=[]),
            reports=MagicMock(),
            write_summary=None,
            runtime_seconds=0.0,
            live=True,
        )

    monkeypatch.setattr("apps.webui.server.routes.spotify.run_import", fake_run_import)
    try:
        pid = "5Ptpplbw51rSiHGWjjuVsy"
        response = client.post(
            f"/api/v1/spotify/playlists/{pid}:import",
            json={"confirm_playlist_id": pid, "force": True},
        )
        assert response.status_code == 202
        opts = captured["options"]
        assert isinstance(opts, ImportOptions)
        assert opts.live is True
        assert opts.max_tracks is None
        assert opts.state_db_path == db_path
        assert opts.force is True
    finally:
        client.close()
        conn.close()


def test_import_running_returns_409(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import apps.webui.server.routes.spotify as spotify_routes

    client, conn = _make_client(tmp_path)
    monkeypatch.setattr(
        SpotifyClient,
        "from_saved",
        classmethod(lambda cls, **kwargs: MagicMock(spec=SpotifyClient)),
    )
    spotify_routes._IMPORT_JOBS.current = spotify_routes._ImportJob(
        playlist_id="5Ptpplbw51rSiHGWjjuVsy",
        phase="running",
    )
    try:
        pid = "5Ptpplbw51rSiHGWjjuVsy"
        response = client.post(
            f"/api/v1/spotify/playlists/{pid}:import",
            json={"confirm_playlist_id": pid},
        )
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "SPOTIFY_IMPORT_RUNNING"
    finally:
        spotify_routes._IMPORT_JOBS.current = None
        client.close()
        conn.close()
