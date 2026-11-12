"""A11Y-11811: reduce_motion ui-pref over HTTP (agent parity with Appearance toggle).

[if] GET omits reduce_motion or it defaults wrong [then] fail, [else stop].
[if] PUT reduce_motion true does not round-trip to ui-prefs.json [then] fail, [else stop].
[if] a non-boolean is accepted [then] fail, [else stop].
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.routes import settings_ai
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = [pytest.mark.requirement("A11Y-11811")]

KEY = "reduce_motion"


@pytest.fixture
def prefs_client(tmp_path: Path) -> Iterator[TestClient]:
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


def test_reduce_motion_defaults_off(prefs_client: TestClient) -> None:
    """[if] a fresh install reads Reduce motion on [then] fail."""
    assert prefs_client.get("/api/v1/ui-prefs").json()[KEY] is False


def test_reduce_motion_put_persists_and_reads_back(prefs_client: TestClient, tmp_path: Path) -> None:
    """[if] PUT reduce_motion true does not persist or disturbs show_pads [then] fail."""
    assert prefs_client.put("/api/v1/ui-prefs", json={"show_pads": False}).status_code == 200
    response = prefs_client.put("/api/v1/ui-prefs", json={KEY: True})
    assert response.status_code == 200, response.text
    assert response.json()[KEY] is True
    on_disk = json.loads((tmp_path / "data" / "state" / "ui-prefs.json").read_text(encoding="utf-8"))
    assert on_disk[KEY] is True
    body = prefs_client.get("/api/v1/ui-prefs").json()
    assert body[KEY] is True
    assert body["show_pads"] is False
    assert prefs_client.put("/api/v1/ui-prefs", json={KEY: False}).json()[KEY] is False


@pytest.mark.parametrize("bad", ["true", 1, 0, "yes", [], {}, None])
def test_reduce_motion_refuses_non_boolean(prefs_client: TestClient, bad: object) -> None:
    """[if] a coerced non-boolean is accepted [then] fail."""
    assert prefs_client.put("/api/v1/ui-prefs", json={KEY: True}).status_code == 200
    response = prefs_client.put("/api/v1/ui-prefs", json={KEY: bad})
    assert response.status_code == 422
    assert prefs_client.get("/api/v1/ui-prefs").json()[KEY] is True


def test_reduce_motion_invalid_on_disk_raises_on_get(prefs_client: TestClient, tmp_path: Path) -> None:
    """[if] ui-prefs.json holds a string reduce_motion [then] GET must 422."""
    prefs_path = tmp_path / "data" / "state" / "ui-prefs.json"
    prefs_path.parent.mkdir(parents=True, exist_ok=True)
    prefs_path.write_text(json.dumps({KEY: "on"}), encoding="utf-8")
    response = prefs_client.get("/api/v1/ui-prefs")
    assert response.status_code == 422
    assert "reduce_motion must be a boolean" in response.text


def test_reduce_motion_agent_settings_ai_key() -> None:
    """[if] reduce_motion is absent from settings AI allowlist [then] agent path is incomplete."""
    assert KEY in settings_ai.ALLOWED_KEYS
