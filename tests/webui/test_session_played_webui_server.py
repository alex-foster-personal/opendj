"""Session played routes on the legacy webui server (issue #13522).

[if] the webui server gets GET /api/v1/session/played [then] it answers 200, [else stop].

The e2e configs boot ``python -m apps.webui.server``, which never passes through
``apps.engine_core.app.create_app``; the played route must answer there too.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.webui.server.app import create_app
from apps.webui.server.session_played import NEW_PATH, PLAYED_PATH

pytestmark = pytest.mark.requirement("RESCUE-13522")


def _client(tmp_path: Path) -> TestClient:
    state_dir = tmp_path / "data" / "state"
    state_dir.mkdir(parents=True)
    app = create_app(state_db_path=str(state_dir / "state.db"), mount_frontend=False)
    return TestClient(app, base_url="http://127.0.0.1")


def test_webui_server_serves_session_played(tmp_path: Path) -> None:
    """[if] the webui server gets GET /api/v1/session/played [then] it answers 200, [else stop]."""
    with _client(tmp_path) as client:
        resp = client.get(PLAYED_PATH)
    assert resp.status_code == 200, f"if the webui server 404s the played route then broken: {resp.text}"
    assert resp.json()["played_ids"] == []


def test_webui_server_stores_played_set_under_its_data_dir(tmp_path: Path) -> None:
    """[if] a play is posted to the webui server [then] it lands under its own data dir, [else stop]."""
    with _client(tmp_path) as client:
        assert client.post(PLAYED_PATH, json={"stable_id": "a1"}).status_code == 200
        assert client.get(PLAYED_PATH).json()["played_ids"] == ["a1"]
        assert client.post(NEW_PATH).json()["played_ids"] == []
    assert (tmp_path / "data" / "state" / "session" / "played.json").is_file()
