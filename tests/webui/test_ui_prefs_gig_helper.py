"""PERFMODE-16 ui-prefs gig_helper field."""

from __future__ import annotations

import json
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


@pytest.mark.requirement("PERFMODE-16")
def test_default_gig_helper_is_unset(prefs_client: TestClient) -> None:
    """[if] ui-prefs is empty [then] GET reports gig_helper unset, [else stop]."""
    body = prefs_client.get("/api/v1/ui-prefs").json()
    assert body["gig_helper"] == "unset"


@pytest.mark.requirement("PERFMODE-16")
def test_put_gig_helper_round_trip(prefs_client: TestClient) -> None:
    """[if] PUT sets on [then] GET round-trips on, [else stop]."""
    response = prefs_client.put("/api/v1/ui-prefs", json={"gig_helper": "on"})
    assert response.status_code == 200
    assert response.json()["gig_helper"] == "on"


@pytest.mark.requirement("PERFMODE-16")
@pytest.mark.parametrize("bad", ["ON", "yes", True, "maybe"])
def test_put_rejects_invalid_gig_helper(prefs_client: TestClient, bad) -> None:
    """[if] PUT sends invalid gig_helper [then] the route returns 422, [else stop]."""
    response = prefs_client.put("/api/v1/ui-prefs", json={"gig_helper": bad})
    assert response.status_code == 422


@pytest.mark.requirement("PERFMODE-16")
def test_old_blob_without_gig_helper_defaults_unset(
    prefs_client: TestClient, tmp_path: Path
) -> None:
    """[if] an old blob lacks gig_helper [then] GET still reports unset, [else stop]."""
    prefs = tmp_path / "data" / "state" / "ui-prefs.json"
    prefs.write_text(json.dumps({"theme": "dark"}) + "\n", encoding="utf-8")
    body = prefs_client.get("/api/v1/ui-prefs").json()
    assert body["gig_helper"] == "unset"
