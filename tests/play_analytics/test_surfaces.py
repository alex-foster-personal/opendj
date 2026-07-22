"""HTTP and CLI parity tests for play analytics."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.play_analytics.api import create_router
from apps.play_analytics.cli import main
from apps.play_analytics.query import query_play_analytics
from apps.webui.server.app import create_app


def test_production_app_registers_play_analytics_contract() -> None:
    paths = create_app(mount_frontend=False).openapi()["paths"]

    assert "/api/play-analytics" in paths


def test_http_returns_query_contract(analytics_db: Path) -> None:
    app = FastAPI()
    app.include_router(create_router(db_path=analytics_db))

    with TestClient(app) as client:
        response = client.get(
            "/api/play-analytics", params={"share_state": "shared_local", "limit": 1}
        )

    assert response.status_code == 200
    assert response.json() == query_play_analytics(
        analytics_db,
        share_state="shared_local",
        limit=1,
    )


def test_http_surfaces_schema_failure(analytics_db: Path) -> None:
    app = FastAPI()
    app.include_router(create_router(db_path=analytics_db.with_name("missing.db")))

    with TestClient(app) as client:
        response = client.get("/api/play-analytics")

    assert response.status_code == 503
    assert "does not exist" in response.json()["detail"]


def test_cli_json_matches_query_contract(analytics_db: Path, capsys) -> None:
    exit_code = main(
        [
            "--db",
            str(analytics_db),
            "--share-state",
            "shared_local",
            "--limit",
            "1",
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == query_play_analytics(
        analytics_db,
        share_state="shared_local",
        limit=1,
    )
