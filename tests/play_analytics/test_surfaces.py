"""HTTP and CLI parity tests for play analytics."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from apps.play_analytics.cli import main
from apps.play_analytics.contract import MISSING_EVENT_STORE_DETAIL_PREFIX
from apps.play_analytics.http import create_router
from apps.play_analytics.query import query_play_analytics
from apps.webui.server.app import create_app
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.play_analytics.helpers import play_analytics_test_client


def test_production_app_registers_play_analytics_contract() -> None:
    paths = create_app(mount_frontend=False).openapi()["paths"]

    assert "/api/play-analytics" in paths


def test_http_returns_query_contract(analytics_db: Path) -> None:
    with play_analytics_test_client(analytics_db) as client:
        response = client.get("/api/play-analytics", params={"share_state": "shared_local", "limit": 1})

    assert response.status_code == 200
    assert response.json() == query_play_analytics(
        analytics_db,
        share_state="shared_local",
        limit=1,
    )


def test_http_surfaces_schema_failure(analytics_db: Path) -> None:
    with play_analytics_test_client(analytics_db.with_name("missing.db")) as client:
        response = client.get("/api/play-analytics")

    assert response.status_code == 503
    assert "does not exist" in response.json()["detail"]


def test_a_missing_store_answers_the_prefix_the_ui_keys_on(analytics_db: Path) -> None:
    with play_analytics_test_client(analytics_db.with_name("missing.db")) as client:
        response = client.get("/api/play-analytics")

    assert response.status_code == 503
    assert response.json()["detail"].startswith(MISSING_EVENT_STORE_DETAIL_PREFIX)


def test_control_a_store_without_its_tables_is_not_read_as_missing(tmp_path: Path) -> None:
    empty_store = tmp_path / "sets.db"
    sqlite3.connect(empty_store).close()
    app = FastAPI()
    app.include_router(create_router(db_path=empty_store))

    with TestClient(app) as client:
        response = client.get("/api/play-analytics")

    assert response.status_code == 503
    assert not response.json()["detail"].startswith(MISSING_EVENT_STORE_DETAIL_PREFIX)


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
