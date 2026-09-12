"""PERFMODE-03 ui-prefs app_posture field.

Regression: missing key must not 500; uppercase must not silently coerce.
"""

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


@pytest.mark.requirement("PERFMODE-03")
def test_default_app_posture_is_prep(prefs_client: TestClient) -> None:
    """[if] ui-prefs is empty [then] GET reports app_posture prep, [else stop]."""
    body = prefs_client.get("/api/v1/ui-prefs").json()
    assert body["app_posture"] == "prep"


@pytest.mark.requirement("PERFMODE-03")
def test_put_app_posture_round_trip(prefs_client: TestClient) -> None:
    """[if] PUT sets gig [then] GET round-trips gig, [else stop]."""
    response = prefs_client.put("/api/v1/ui-prefs", json={"app_posture": "gig"})
    assert response.status_code == 200
    assert response.json()["app_posture"] == "gig"


@pytest.mark.requirement("PERFMODE-03")
@pytest.mark.parametrize("bad", ["GIG", "1", True, "practice", "performance"])
def test_put_rejects_invalid_app_posture(prefs_client: TestClient, bad) -> None:
    """[if] PUT sends invalid app_posture [then] the route returns 422, [else stop]."""
    response = prefs_client.put("/api/v1/ui-prefs", json={"app_posture": bad})
    assert response.status_code == 422


@pytest.mark.requirement("PERFMODE-03")
def test_old_blob_without_app_posture_defaults_prep(
    prefs_client: TestClient, tmp_path: Path
) -> None:
    """[if] an old blob lacks app_posture [then] GET still reports prep, [else stop]."""
    prefs = tmp_path / "data" / "state" / "ui-prefs.json"
    prefs.write_text(json.dumps({"theme": "dark"}) + "\n", encoding="utf-8")
    body = prefs_client.get("/api/v1/ui-prefs").json()
    assert body["app_posture"] == "prep"
