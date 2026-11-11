"""Wave height pref over HTTP (WAVEQS-11733, issue #10250).

[if] wave_height is PUT [then] it persists, defaults full, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = pytest.mark.requirement("WAVEQS-11733")


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


def _prefs_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "state" / "ui-prefs.json"


def _disk(tmp_path: Path) -> dict[str, object]:
    return json.loads(_prefs_path(tmp_path).read_text())


def test_wave_height_defaults_full(prefs_client: TestClient) -> None:
    """[if] ui-prefs are fetched with nothing stored [then] wave_height is full, [else stop]."""
    r = prefs_client.get("/api/v1/ui-prefs")
    assert r.status_code == 200
    assert r.json()["wave_height"] == "full"


def test_wave_height_put_round_trips_through_disk(prefs_client: TestClient, tmp_path: Path) -> None:
    """[if] PUT names wave_height half [then] the reply, disk and a later GET carry it, [else stop]."""
    r = prefs_client.put("/api/v1/ui-prefs", json={"wave_height": "half"})
    assert r.status_code == 200
    assert r.json()["wave_height"] == "half"
    assert _disk(tmp_path)["wave_height"] == "half"
    got = prefs_client.get("/api/v1/ui-prefs").json()
    assert got["wave_height"] == "half"


def test_wave_height_put_rejects_unknown(prefs_client: TestClient, tmp_path: Path) -> None:
    """[if] PUT carries an unknown wave_height [then] 422 and disk unchanged, [else stop]."""
    r = prefs_client.put("/api/v1/ui-prefs", json={"wave_height": "quarter"})
    assert r.status_code == 422, r.text
    assert not _prefs_path(tmp_path).exists()


def test_wave_height_put_of_one_key_leaves_the_other(prefs_client: TestClient) -> None:
    """[if] a PUT names only tempo_lane_scale [then] wave_height keeps full, [else stop]."""
    prefs_client.put("/api/v1/ui-prefs", json={"wave_height": "half"})
    r = prefs_client.put("/api/v1/ui-prefs", json={"tempo_lane_scale": "relative"})
    assert r.json()["wave_height"] == "half"
    assert r.json()["tempo_lane_scale"] == "relative"


def test_wave_height_rejects_malformed_on_disk(prefs_client: TestClient, tmp_path: Path) -> None:
    """[if] wave_height on disk is malformed [then] GET returns 422 UI_PREFS_INVALID, [else stop]."""
    path = _prefs_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"wave_height": "tall"}))
    r = prefs_client.get("/api/v1/ui-prefs")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "UI_PREFS_INVALID"
    assert "wave_height" in r.json()["detail"]["message"]
