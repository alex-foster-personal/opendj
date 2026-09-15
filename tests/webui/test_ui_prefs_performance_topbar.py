"""Issue #2853: Beat Sync max and AutoPlay toggles over HTTP (agent parity).

Every TopBar control in this set drives one of these keys, so PUT /api/v1/ui-prefs is
the programmatic twin. Regression lines:
- if a key is absent from GET defaults then agents cannot read the live value
- if a PUT does not land in ui-prefs.json then the toggle is decorative
- if a wrong-typed key is on disk then GET refuses rather than coercing
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend

TOPBAR_DEFAULTS: dict[str, Any] = {
    "beat_sync_max": True,
    "auto_play_enabled": True,
    "auto_play_enforce_order": False,
    "auto_play_maximize_reach": True,
}
TOPBAR_WRITES: dict[str, Any] = {
    "beat_sync_max": False,
    "auto_play_enabled": False,
    "auto_play_enforce_order": True,
    "auto_play_maximize_reach": False,
}


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


def test_ui_prefs_defaults_include_the_four_topbar_prefs(prefs_client: TestClient) -> None:
    body = prefs_client.get("/api/v1/ui-prefs").json()
    for key, want in TOPBAR_DEFAULTS.items():
        assert body[key] == want, key


@pytest.mark.parametrize("key", tuple(TOPBAR_WRITES))
def test_ui_prefs_topbar_key_round_trips_to_disk(
    prefs_client: TestClient, tmp_path: Path, key: str
) -> None:
    value = TOPBAR_WRITES[key]
    r = prefs_client.put("/api/v1/ui-prefs", json={key: value})
    assert r.status_code == 200
    assert r.json()[key] == value
    on_disk = json.loads((tmp_path / "data" / "state" / "ui-prefs.json").read_text())
    assert on_disk[key] == value
    assert prefs_client.get("/api/v1/ui-prefs").json()[key] == value


def test_ui_prefs_topbar_put_leaves_the_other_three_alone(prefs_client: TestClient) -> None:
    assert prefs_client.put("/api/v1/ui-prefs", json={"beat_sync_max": False}).status_code == 200
    body = prefs_client.get("/api/v1/ui-prefs").json()
    assert body["beat_sync_max"] is False
    assert body["auto_play_enabled"] is TOPBAR_DEFAULTS["auto_play_enabled"]
    assert body["auto_play_enforce_order"] is TOPBAR_DEFAULTS["auto_play_enforce_order"]
    assert body["auto_play_maximize_reach"] is TOPBAR_DEFAULTS["auto_play_maximize_reach"]


@pytest.mark.parametrize("key", tuple(TOPBAR_DEFAULTS))
def test_ui_prefs_rejects_non_boolean_topbar_toggle(prefs_client: TestClient, key: str) -> None:
    r = prefs_client.put("/api/v1/ui-prefs", json={key: "maybe"})
    assert r.status_code == 422


def test_ui_prefs_reads_a_blob_written_before_the_topbar_keys_existed(
    prefs_client: TestClient, tmp_path: Path
) -> None:
    path = tmp_path / "data" / "state" / "ui-prefs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"theme": "dark", "confirm": {}}) + "\n", encoding="utf-8")
    body = prefs_client.get("/api/v1/ui-prefs").json()
    for key, want in TOPBAR_DEFAULTS.items():
        assert body[key] == want, key


@pytest.mark.parametrize("key", tuple(TOPBAR_DEFAULTS))
def test_ui_prefs_refuses_a_wrong_typed_topbar_key_on_disk(
    prefs_client: TestClient, tmp_path: Path, key: str
) -> None:
    path = tmp_path / "data" / "state" / "ui-prefs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"theme": "dark", key: "yes"}) + "\n", encoding="utf-8")
    assert prefs_client.get("/api/v1/ui-prefs").status_code == 422
