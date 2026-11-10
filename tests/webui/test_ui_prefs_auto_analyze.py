"""ANALYSIS-11724: per-lane analyze-on-import ui-prefs over HTTP."""

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


def _disk(tmp_path: Path) -> dict[str, object]:
    return json.loads((tmp_path / "data" / "state" / "ui-prefs.json").read_text())


@pytest.mark.requirement("ANALYSIS-11724")
def test_auto_analyze_defaults_on_on_on_off(prefs_client: TestClient) -> None:
    """[if] ui-prefs are fetched fresh [then] lanes default on on on off, [else stop]."""
    body = prefs_client.get("/api/v1/ui-prefs").json()
    assert body["auto_analyze_grid"] is True
    assert body["auto_analyze_key"] is True
    assert body["auto_analyze_sections"] is True
    assert body["auto_analyze_vocals"] is False


@pytest.mark.requirement("ANALYSIS-11724")
@pytest.mark.parametrize(
    "key",
    [
        "auto_analyze_grid",
        "auto_analyze_key",
        "auto_analyze_sections",
        "auto_analyze_vocals",
    ],
)
def test_auto_analyze_put_round_trips(prefs_client: TestClient, tmp_path: Path, key: str) -> None:
    """[if] a lane pref is PUT [then] GET and disk carry it, [else stop]."""
    r = prefs_client.put("/api/v1/ui-prefs", json={key: False})
    assert r.status_code == 200
    assert r.json()[key] is False
    assert _disk(tmp_path)[key] is False
    assert prefs_client.get("/api/v1/ui-prefs").json()[key] is False


@pytest.mark.requirement("ANALYSIS-11724")
@pytest.mark.parametrize("bad", ["yes", 1])
def test_auto_analyze_rejects_non_boolean(prefs_client: TestClient, bad: object) -> None:
    """[if] a non-boolean is PUT for auto_analyze_grid [then] the response is 422, [else stop]."""
    assert prefs_client.put("/api/v1/ui-prefs", json={"auto_analyze_grid": bad}).status_code == 422


@pytest.mark.requirement("ANALYSIS-11724")
def test_auto_analyze_on_disk_non_bool_refused(prefs_client: TestClient, tmp_path: Path) -> None:
    """[if] disk stores a non-boolean lane pref [then] GET is 422, [else stop]."""
    path = tmp_path / "data" / "state" / "ui-prefs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "confirm": {},
                "theme": "dark",
                "auto_analyze_grid": "on",
            }
        ),
        encoding="utf-8",
    )
    assert prefs_client.get("/api/v1/ui-prefs").status_code == 422
