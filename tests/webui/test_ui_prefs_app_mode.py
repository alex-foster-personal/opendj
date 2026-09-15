"""PERFMODE-11 ui-prefs app_mode.last_gig_at field."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend


@pytest.fixture
def prefs_client(tmp_path: Path) -> TestClient:
    data_dir = tmp_path / "data"
    state_path = data_dir / "state" / "state.db"
    state_db.open_rw(state_path).close()
    app = create_app(
        backend=SqliteBackend(state_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        mount_frontend=False,
        state_db_path=str(state_path),
    )
    app.state.data_dir = data_dir
    with TestClient(app) as client:
        yield client


@pytest.mark.requirement("PERFMODE-11")
def test_default_last_gig_at_is_null(prefs_client: TestClient) -> None:
    body = prefs_client.get("/api/v1/ui-prefs").json()
    assert body["app_mode"] == {"last_gig_at": None}


@pytest.mark.requirement("PERFMODE-11")
def test_put_last_gig_at_round_trip(prefs_client: TestClient) -> None:
    stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    response = prefs_client.put("/api/v1/ui-prefs", json={"app_mode": {"last_gig_at": stamp}})
    assert response.status_code == 200
    assert response.json()["app_mode"]["last_gig_at"] is not None


@pytest.mark.requirement("PERFMODE-11")
@pytest.mark.parametrize("bad", ["not-iso", True, 1, "2026-09-15T12:00:00"])
def test_put_rejects_invalid_last_gig_at(prefs_client: TestClient, bad) -> None:
    response = prefs_client.put("/api/v1/ui-prefs", json={"app_mode": {"last_gig_at": bad}})
    assert response.status_code == 422


@pytest.mark.requirement("PERFMODE-11")
def test_old_blob_without_app_mode_defaults_null(prefs_client: TestClient, tmp_path: Path) -> None:
    prefs = tmp_path / "data" / "state" / "ui-prefs.json"
    prefs.write_text(json.dumps({"theme": "dark"}) + "\n", encoding="utf-8")
    body = prefs_client.get("/api/v1/ui-prefs").json()
    assert body["app_mode"] == {"last_gig_at": None}


@pytest.mark.requirement("PERFMODE-11")
def test_malformed_last_gig_at_on_disk_returns_422(
    prefs_client: TestClient, tmp_path: Path
) -> None:
    prefs = tmp_path / "data" / "state" / "ui-prefs.json"
    prefs.write_text(
        json.dumps({"app_mode": {"last_gig_at": "not-a-timestamp"}}) + "\n",
        encoding="utf-8",
    )
    response = prefs_client.get("/api/v1/ui-prefs")
    assert response.status_code == 422
