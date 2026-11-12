"""ui-prefs lighting.enabled and lighting.protocol (issue #11774, agent parity).

Agents drive the same keys via ``opendj api PUT /api/v1/ui-prefs`` with a JSON
``lighting`` object. Storage only; no UDP output loop here.

- if fresh GET lacks lighting defaults then agents cannot discover the off state
- if PUT does not land in ui-prefs.json then the control is decorative
- if an unknown protocol or enabled-with-off is accepted then output misbehaves

[if] lighting keys are missing, wrong by default, not persisted, or accept bad values [then] output misbehaves, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend

REQ_ID = "LIGHTING-11774"
pytestmark = pytest.mark.requirement(REQ_ID)


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
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.requirement(REQ_ID)
def test_lighting_defaults_on_fresh_get(prefs_client: TestClient) -> None:
    """[if] fresh GET ui-prefs [then] lighting defaults to off and disabled, [else stop]."""
    body = prefs_client.get("/api/v1/ui-prefs").json()
    assert body["lighting"] == {"enabled": False, "protocol": "off"}


@pytest.mark.requirement(REQ_ID)
def test_lighting_round_trips_through_disk(prefs_client: TestClient, tmp_path: Path) -> None:
    """[if] lighting PUT with artnet [then] GET and ui-prefs.json match, [else stop]."""
    payload = {"lighting": {"enabled": True, "protocol": "artnet"}}
    r = prefs_client.put("/api/v1/ui-prefs", json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["lighting"] == {"enabled": True, "protocol": "artnet"}
    assert _disk(tmp_path)["lighting"] == {"enabled": True, "protocol": "artnet"}
    assert prefs_client.get("/api/v1/ui-prefs").json()["lighting"] == {
        "enabled": True,
        "protocol": "artnet",
    }
    for protocol in ("sacn", "osc"):
        r = prefs_client.put("/api/v1/ui-prefs", json={"lighting": {"protocol": protocol}})
        assert r.status_code == 200, r.text
        assert r.json()["lighting"]["protocol"] == protocol


@pytest.mark.requirement(REQ_ID)
def test_lighting_rejects_unknown_protocol(prefs_client: TestClient) -> None:
    """[if] PUT names dmx-usb protocol [then] 422 lists allowed enum values, [else stop]."""
    r = prefs_client.put("/api/v1/ui-prefs", json={"lighting": {"protocol": "dmx-usb"}})
    assert r.status_code == 422, r.text
    text = json.dumps(r.json())
    for token in ("off", "artnet", "sacn", "osc"):
        assert token in text


@pytest.mark.requirement(REQ_ID)
def test_lighting_rejects_enabled_with_protocol_off(prefs_client: TestClient) -> None:
    """[if] PUT enables lighting with protocol off [then] 422 names protocol_required, [else stop]."""
    r = prefs_client.put("/api/v1/ui-prefs", json={"lighting": {"enabled": True, "protocol": "off"}})
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    text = json.dumps(detail) if isinstance(detail, list) else detail.get("message", json.dumps(detail))
    assert "protocol_required_when_enabled" in text


@pytest.mark.requirement(REQ_ID)
def test_lighting_partial_put_merges(prefs_client: TestClient) -> None:
    """[if] only protocol is PATCHed [then] enabled stays from storage, [else stop]."""
    assert (
        prefs_client.put(
            "/api/v1/ui-prefs",
            json={"lighting": {"enabled": True, "protocol": "artnet"}},
        ).status_code
        == 200
    )
    r = prefs_client.put("/api/v1/ui-prefs", json={"lighting": {"protocol": "sacn"}})
    assert r.status_code == 200, r.text
    assert r.json()["lighting"] == {"enabled": True, "protocol": "sacn"}


@pytest.mark.requirement(REQ_ID)
def test_lighting_enabled_only_when_stored_protocol_off(prefs_client: TestClient) -> None:
    """[if] PUT enables only while stored protocol is off [then] 422 protocol_required, [else stop]."""
    body = prefs_client.get("/api/v1/ui-prefs").json()
    assert body["lighting"]["protocol"] == "off"
    r = prefs_client.put("/api/v1/ui-prefs", json={"lighting": {"enabled": True}})
    assert r.status_code == 422, r.text
    assert "protocol_required_when_enabled" in json.dumps(r.json())
