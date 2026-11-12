"""keyboard_preset over HTTP (KEYB-11488, agent parity).

[if] GET lacks keyboard.preset default opendj [then] fail, [else stop].
[if] PUT preset does not persist to ui-prefs.json [then] fail, [else stop].
[if] invalid keyboard shape is accepted [then] fail, [else stop].

Regression lines:
- if GET lacks keyboard.preset opendj default then agents start on wrong layout
- if PUT preset does not persist to ui-prefs.json then layout choice is decorative
- if invalid preset or keyboard shape is accepted then agents cannot trust PUT
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = pytest.mark.requirement("KEYB-11488")


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
    path = tmp_path / "data" / "state" / "ui-prefs.json"
    return json.loads(path.read_text())


def test_keyboard_defaults_to_opendj(prefs_client: TestClient) -> None:
    body = prefs_client.get("/api/v1/ui-prefs").json()
    assert body["keyboard"] == {"preset": "opendj", "overrides": {}}


def test_keyboard_preset_round_trip(prefs_client: TestClient, tmp_path: Path) -> None:
    r = prefs_client.put("/api/v1/ui-prefs", json={"keyboard": {"preset": "rekordbox"}})
    assert r.status_code == 200, r.text
    assert r.json()["keyboard"]["preset"] == "rekordbox"
    assert prefs_client.get("/api/v1/ui-prefs").json()["keyboard"]["preset"] == "rekordbox"
    assert _disk(tmp_path)["keyboard"]["preset"] == "rekordbox"


def test_keyboard_preset_switch_keeps_overrides(prefs_client: TestClient) -> None:
    prefs_client.put(
        "/api/v1/ui-prefs",
        json={"keyboard": {"overrides": {"deck.1.play": ["Enter"]}}},
    )
    prefs_client.put("/api/v1/ui-prefs", json={"keyboard": {"preset": "serato"}})
    body = prefs_client.get("/api/v1/ui-prefs").json()
    assert body["keyboard"]["preset"] == "serato"
    assert body["keyboard"]["overrides"] == {"deck.1.play": ["Enter"]}


@pytest.mark.parametrize(
    "payload",
    [
        {"keyboard": "x"},
        {"keyboard": {"preset": "traktor"}},
        {"keyboard": {"overrides": {"a": "Space"}}},
        {"keyboard": {"preset": "opendj", "extra": 1}},
    ],
)
def test_keyboard_refuses_invalid(prefs_client: TestClient, payload: dict[str, object]) -> None:
    r = prefs_client.put("/api/v1/ui-prefs", json=payload)
    assert r.status_code == 422
