"""Beat grid lines pref over HTTP (agent parity, issue #10244).

[if] wave_grid_lines is PUT [then] it persists and defaults beats_bars, [else stop].

The Settings row and the waveform quick-settings popover drive this key, so
PUT /api/v1/ui-prefs is their programmatic twin. Mirrors
test_ui_prefs_tempo_lane.py.
Regression lines:
- if wave_grid_lines is absent from GET defaults then the deck boots on nothing
- if a PUT does not land in ui-prefs.json then the control is decorative
- if an unknown value is coerced rather than refused then a typo hides the grid
- if a PUT of another key resets it then two controls fight
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = pytest.mark.requirement("WAVEQS-11736")


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


def test_wave_grid_lines_defaults_to_beats_bars(prefs_client: TestClient) -> None:
    """[if] ui-prefs are fetched with nothing stored [then] the grid is beats_bars, [else stop]."""
    r = prefs_client.get("/api/v1/ui-prefs")
    assert r.status_code == 200
    assert r.json()["wave_grid_lines"] == "beats_bars"


def test_wave_grid_lines_put_round_trips_through_disk(prefs_client: TestClient, tmp_path: Path) -> None:
    """[if] each value is PUT [then] the reply, disk and a later GET carry it, [else stop]."""
    for value in ("bars", "off", "beats_bars"):
        r = prefs_client.put("/api/v1/ui-prefs", json={"wave_grid_lines": value})
        assert r.status_code == 200, r.text
        assert r.json()["wave_grid_lines"] == value
        assert _disk(tmp_path)["wave_grid_lines"] == value
        assert prefs_client.get("/api/v1/ui-prefs").json()["wave_grid_lines"] == value


@pytest.mark.parametrize("bad", ["dots", "beats", 1, True])
def test_wave_grid_lines_put_rejects_unknown(prefs_client: TestClient, tmp_path: Path, bad: object) -> None:
    """[if] a PUT carries an unknown value [then] 422 and disk unchanged, [else stop]."""
    r = prefs_client.put("/api/v1/ui-prefs", json={"wave_grid_lines": bad})
    assert r.status_code == 422, r.text
    assert not _prefs_path(tmp_path).exists(), "a refused PUT must not write ui-prefs.json"


def test_wave_grid_lines_survives_a_put_of_another_key(prefs_client: TestClient) -> None:
    """[if] another key is PUT after bars [then] the grid stays bars, [else stop]."""
    assert prefs_client.put("/api/v1/ui-prefs", json={"wave_grid_lines": "bars"}).status_code == 200
    assert prefs_client.put("/api/v1/ui-prefs", json={"tempo_lane_scale": "relative"}).status_code == 200
    assert prefs_client.get("/api/v1/ui-prefs").json()["wave_grid_lines"] == "bars"


def test_wave_grid_lines_rejects_malformed_on_disk(prefs_client: TestClient, tmp_path: Path) -> None:
    """[if] the stored value is malformed [then] GET returns 422 UI_PREFS_INVALID, [else stop]."""
    path = _prefs_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"wave_grid_lines": "dots"}))
    r = prefs_client.get("/api/v1/ui-prefs")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "UI_PREFS_INVALID"
    assert "wave_grid_lines" in r.json()["detail"]["message"]
