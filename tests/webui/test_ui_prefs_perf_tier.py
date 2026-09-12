"""PERFMODE-01 ui-prefs perf_tier field."""

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


@pytest.mark.requirement("PERFMODE-01")
def test_default_perf_tier_is_auto(prefs_client: TestClient) -> None:
    body = prefs_client.get("/api/v1/ui-prefs").json()
    assert body["perf_tier"] == "auto"


@pytest.mark.requirement("PERFMODE-01")
def test_put_perf_tier_round_trip(prefs_client: TestClient) -> None:
    response = prefs_client.put("/api/v1/ui-prefs", json={"perf_tier": "high"})
    assert response.status_code == 200
    assert response.json()["perf_tier"] == "high"


@pytest.mark.requirement("PERFMODE-01")
@pytest.mark.parametrize("bad", ["AUTO", "1", True])
def test_put_rejects_invalid_perf_tier(prefs_client: TestClient, bad) -> None:
    response = prefs_client.put("/api/v1/ui-prefs", json={"perf_tier": bad})
    assert response.status_code == 422


@pytest.mark.requirement("PERFMODE-01")
def test_old_blob_without_perf_tier_defaults_auto(
    prefs_client: TestClient, tmp_path: Path
) -> None:
    prefs = tmp_path / "data" / "state" / "ui-prefs.json"
    prefs.write_text(json.dumps({"theme": "dark"}) + "\n", encoding="utf-8")
    body = prefs_client.get("/api/v1/ui-prefs").json()
    assert body["perf_tier"] == "auto"
