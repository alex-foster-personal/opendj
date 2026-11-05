"""speaker_auto_recover over HTTP (AUDIO-DEVICE-06, agent parity).

- if speaker_auto_recover is absent from GET defaults then the overlay boots on nothing
- if it does not default to auto then a gig gets an unasked-for Bluetooth drop
- if a PUT does not land in ui-prefs.json then the control is decorative
- if an unknown value is coerced rather than refused then a typo changes recovery

[if] speaker_auto_recover is missing, not auto by default, not persisted, or coerces an unknown value [then] recovery silently misbehaves, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = pytest.mark.requirement("AUDIO-DEVICE-06")


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


def test_speaker_auto_recover_defaults_to_auto(prefs_client: TestClient) -> None:
    body = prefs_client.get("/api/v1/ui-prefs").json()
    assert body["speaker_auto_recover"] == "auto", "if the default is not auto then gig posture may auto-drop the speaker"


def test_speaker_auto_recover_round_trips_through_disk(prefs_client: TestClient, tmp_path: Path) -> None:
    for value in ("off", "on", "auto"):
        r = prefs_client.put("/api/v1/ui-prefs", json={"speaker_auto_recover": value})
        assert r.status_code == 200, r.text
        assert r.json()["speaker_auto_recover"] == value
        assert _disk(tmp_path)["speaker_auto_recover"] == value
        assert prefs_client.get("/api/v1/ui-prefs").json()["speaker_auto_recover"] == value


def test_speaker_auto_recover_refuses_unknown_values(prefs_client: TestClient) -> None:
    r = prefs_client.put("/api/v1/ui-prefs", json={"speaker_auto_recover": "sometimes"})
    assert r.status_code == 422, "if an unknown value is accepted then a typo silently changes recovery"
