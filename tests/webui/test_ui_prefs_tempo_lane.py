"""Tempo-lane prefs over HTTP (agent parity, PR #8342).

[if] a tempo-lane pref is PUT [then] it persists, defaults on/fixed_10, [else stop].

`show_tempo_lane` hides or shows the deck strip's tempo lane and the main
waveform's tempo curve; `tempo_lane_scale` picks what the curve's full height
means. The settings overlay drives both keys, so PUT /api/v1/ui-prefs is the
programmatic twin of those controls. Mirrors test_ui_prefs_show_sections.py.
Regression lines:
- if either key is absent from GET defaults then the overlay boots on nothing
- if show_tempo_lane defaults off then nobody ever sees the lane
- if a PUT does not land in ui-prefs.json then the control is decorative
- if a non-boolean or an unknown scale is coerced rather than refused then a
  typo silently changes the lane
- if a PUT of one key resets the other then the two controls fight
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = pytest.mark.requirement("TEMPOLANE-8342")


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


def test_tempo_lane_defaults_on_and_fixed_10(prefs_client: TestClient) -> None:
    """[if] ui-prefs are fetched with nothing stored [then] the lane is on at fixed_10, [else stop]."""
    r = prefs_client.get("/api/v1/ui-prefs")
    assert r.status_code == 200
    body = r.json()
    assert body["show_tempo_lane"] is True
    assert body["tempo_lane_scale"] == "fixed_10"


def test_tempo_lane_put_round_trips_through_disk(prefs_client: TestClient, tmp_path: Path) -> None:
    """[if] a PUT names both keys [then] the reply, disk and a later GET carry them, [else stop]."""
    r = prefs_client.put("/api/v1/ui-prefs", json={"show_tempo_lane": False, "tempo_lane_scale": "relative"})
    assert r.status_code == 200
    assert r.json()["show_tempo_lane"] is False
    assert r.json()["tempo_lane_scale"] == "relative"
    assert _disk(tmp_path)["show_tempo_lane"] is False
    assert _disk(tmp_path)["tempo_lane_scale"] == "relative"
    got = prefs_client.get("/api/v1/ui-prefs").json()
    assert (got["show_tempo_lane"], got["tempo_lane_scale"]) == (False, "relative")

    # And back: the overshoot of "changing it works" is a key that never returns.
    r = prefs_client.put("/api/v1/ui-prefs", json={"show_tempo_lane": True, "tempo_lane_scale": "fixed_10"})
    assert (r.json()["show_tempo_lane"], r.json()["tempo_lane_scale"]) == (True, "fixed_10")


def test_tempo_lane_put_of_one_key_leaves_the_other(prefs_client: TestClient) -> None:
    """[if] a PUT names one tempo-lane key [then] the other and show_sections keep theirs, [else stop]."""
    prefs_client.put("/api/v1/ui-prefs", json={"tempo_lane_scale": "relative", "show_sections": True})
    r = prefs_client.put("/api/v1/ui-prefs", json={"show_tempo_lane": False})
    assert r.json()["tempo_lane_scale"] == "relative"
    assert r.json()["show_sections"] is True
    r = prefs_client.put("/api/v1/ui-prefs", json={"tempo_lane_scale": "fixed_10"})
    assert r.json()["show_tempo_lane"] is False


@pytest.mark.parametrize(
    "patch",
    [{"show_tempo_lane": "yes"}, {"show_tempo_lane": 1}, {"tempo_lane_scale": "fixed_5"}, {"tempo_lane_scale": 10}],
)
def test_tempo_lane_put_rejects_malformed(prefs_client: TestClient, tmp_path: Path, patch: dict[str, object]) -> None:
    """[if] a PUT carries a malformed tempo-lane value [then] 422 and disk unchanged, [else stop]."""
    r = prefs_client.put("/api/v1/ui-prefs", json=patch)
    assert r.status_code == 422, r.text
    assert not _prefs_path(tmp_path).exists(), "a refused PUT must not write ui-prefs.json"


@pytest.mark.parametrize(
    ("stored", "key"),
    [({"show_tempo_lane": "yes"}, "show_tempo_lane"), ({"tempo_lane_scale": "loud"}, "tempo_lane_scale")],
)
def test_tempo_lane_rejects_malformed_on_disk(
    prefs_client: TestClient, tmp_path: Path, stored: dict[str, object], key: str
) -> None:
    """[if] a tempo-lane value on disk is malformed [then] GET returns 422 UI_PREFS_INVALID, [else stop]."""
    path = _prefs_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stored))
    r = prefs_client.get("/api/v1/ui-prefs")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "UI_PREFS_INVALID"
    assert key in r.json()["detail"]["message"]
