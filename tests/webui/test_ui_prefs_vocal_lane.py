"""Vocal-lane pref over HTTP (issue #10238, agent parity).

[if] show_vocal_lane is PUT [then] it persists and defaults on, [else stop].

`show_vocal_lane` hides or shows the blue vocal bars on deck waveform rows and
the deck overview strip. Mirrors test_ui_prefs_tempo_lane.py.
Regression lines:
- if show_vocal_lane is absent from GET defaults then the overlay boots wrong
- if show_vocal_lane defaults off then users lose vocal bars silently
- if a PUT does not land in ui-prefs.json then the control is decorative
- if a non-boolean is coerced rather than refused then a typo silently hides bars
- if a PUT of show_vocal_lane resets other overlay keys then controls fight
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = pytest.mark.requirement("WAVEQS-11734")


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


def test_vocal_lane_defaults_on(prefs_client: TestClient) -> None:
    """[if] ui-prefs are fetched with nothing stored [then] the vocal lane is on, [else stop]."""
    r = prefs_client.get("/api/v1/ui-prefs")
    assert r.status_code == 200
    assert r.json()["show_vocal_lane"] is True


def test_vocal_lane_put_round_trips_through_disk(prefs_client: TestClient, tmp_path: Path) -> None:
    """[if] a PUT names show_vocal_lane [then] the reply, disk and a later GET carry it, [else stop]."""
    r = prefs_client.put("/api/v1/ui-prefs", json={"show_vocal_lane": False})
    assert r.status_code == 200
    assert r.json()["show_vocal_lane"] is False
    assert _disk(tmp_path)["show_vocal_lane"] is False
    got = prefs_client.get("/api/v1/ui-prefs").json()
    assert got["show_vocal_lane"] is False

    r = prefs_client.put("/api/v1/ui-prefs", json={"show_vocal_lane": True})
    assert r.json()["show_vocal_lane"] is True


def test_vocal_lane_put_of_one_key_leaves_the_others(prefs_client: TestClient) -> None:
    """[if] a PUT names only show_vocal_lane [then] show_tempo_lane and show_sections keep theirs, [else stop]."""
    prefs_client.put(
        "/api/v1/ui-prefs",
        json={"show_tempo_lane": False, "show_sections": True},
    )
    r = prefs_client.put("/api/v1/ui-prefs", json={"show_vocal_lane": False})
    assert r.json()["show_tempo_lane"] is False
    assert r.json()["show_sections"] is True
    r = prefs_client.put("/api/v1/ui-prefs", json={"show_vocal_lane": True})
    assert r.json()["show_tempo_lane"] is False


@pytest.mark.parametrize("patch", [{"show_vocal_lane": "yes"}, {"show_vocal_lane": 1}])
def test_vocal_lane_put_rejects_malformed(prefs_client: TestClient, tmp_path: Path, patch: dict[str, object]) -> None:
    """[if] a PUT carries a malformed show_vocal_lane value [then] 422 and disk unchanged, [else stop]."""
    r = prefs_client.put("/api/v1/ui-prefs", json=patch)
    assert r.status_code == 422, r.text
    assert not _prefs_path(tmp_path).exists(), "a refused PUT must not write ui-prefs.json"


def test_vocal_lane_rejects_malformed_on_disk(prefs_client: TestClient, tmp_path: Path) -> None:
    """[if] show_vocal_lane on disk is malformed [then] GET returns 422 UI_PREFS_INVALID, [else stop]."""
    path = _prefs_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"show_vocal_lane": "yes"}))
    r = prefs_client.get("/api/v1/ui-prefs")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "UI_PREFS_INVALID"
    assert "show_vocal_lane" in r.json()["detail"]["message"]
