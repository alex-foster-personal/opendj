"""STEMPERF wave_stem_follow pref over HTTP (rekordbox ACTIVE STEM waveform, mimicked).

[if] wave_stem_follow is PUT over HTTP [then] it persists and defaults off, [else stop].
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


def _disk(tmp_path: Path) -> dict[str, object]:
    return json.loads((tmp_path / "data" / "state" / "ui-prefs.json").read_text())


@pytest.mark.requirement("STEMPERF-14288")
def test_wave_stem_follow_defaults_false_and_round_trips(prefs_client: TestClient, tmp_path: Path) -> None:
    """[if] wave_stem_follow is unset then PUT on and off [then] it defaults false and round-trips via disk, [else stop]."""
    assert prefs_client.get("/api/v1/ui-prefs").json()["wave_stem_follow"] is False
    r = prefs_client.put("/api/v1/ui-prefs", json={"wave_stem_follow": True})
    assert r.status_code == 200
    assert r.json()["wave_stem_follow"] is True
    assert _disk(tmp_path)["wave_stem_follow"] is True
    assert prefs_client.get("/api/v1/ui-prefs").json()["wave_stem_follow"] is True
    r = prefs_client.put("/api/v1/ui-prefs", json={"wave_stem_follow": False})
    assert r.json()["wave_stem_follow"] is False
