"""DECKUX-10470: per-deck beat jump size (`beat_jump_sizes`) over HTTP.

Regression lines:
- if GET defaults are not four 4s then a fresh install boots with the wrong jump size
- if a partial PUT wipes the other decks then one stepper press resets three decks
- if an illegal size (3, true, 128) is accepted then the deck stepper gets a size it cannot show
- if the value does not reach ui-prefs.json then it is lost on restart
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend

KEY = "beat_jump_sizes"


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


def test_beat_jump_sizes_default_to_four_beats(prefs_client: TestClient) -> None:
    body = prefs_client.get("/api/v1/ui-prefs").json()
    assert body[KEY] == {"deck_1": 4, "deck_2": 4, "deck_3": 4, "deck_4": 4}, "if defaults are not 4 then broken"


def test_beat_jump_sizes_partial_put_round_trips_to_disk(prefs_client: TestClient, tmp_path: Path) -> None:
    r = prefs_client.put("/api/v1/ui-prefs", json={KEY: {"deck_3": 0.5}})
    assert r.status_code == 200, r.text
    assert r.json()[KEY] == {"deck_1": 4, "deck_2": 4, "deck_3": 0.5, "deck_4": 4}
    r = prefs_client.put("/api/v1/ui-prefs", json={KEY: {"deck_1": 64}})
    assert r.json()[KEY]["deck_3"] == 0.5, "if a partial PUT wipes another deck then broken"
    on_disk = json.loads((tmp_path / "data" / "state" / "ui-prefs.json").read_text())
    assert on_disk[KEY] == {"deck_1": 64, "deck_2": 4, "deck_3": 0.5, "deck_4": 4}
    assert prefs_client.get("/api/v1/ui-prefs").json()[KEY]["deck_1"] == 64


@pytest.mark.parametrize("value", [3, True, 128, "4", 0])
def test_beat_jump_sizes_reject_illegal_size(prefs_client: TestClient, value: object) -> None:
    r = prefs_client.put("/api/v1/ui-prefs", json={KEY: {"deck_2": value}})
    assert r.status_code == 422, f"if size {value!r} is accepted then broken"
