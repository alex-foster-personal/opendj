"""PLATFORM-12937: GET /api/v1/health/live is a liveness verdict, not readiness.

[if] the state database cannot answer [then] the liveness route still says alive, [else stop].
[if] optional cloud probes fail [then] the liveness route still says alive, [else stop].
[if] the liveness body is read [then] it holds no database path or environment value, [else stop].
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from apps.webui.server.backend import InMemoryBackend
from apps.webui.server.deps import get_read_state
from tests.webui.app_fixtures import client_fixture  # noqa: F401  (pytest fixture)

pytestmark = pytest.mark.requirement("PLATFORM-12937")

LIVE_PATH = "/api/v1/health/live"


def _boom() -> object:
    """Stand in for a probe or backend that cannot answer."""
    raise RuntimeError("state.db is wedged")


def test_liveness_answers_when_state_db_cannot(client: TestClient) -> None:
    """Liveness stays 200 while the state backend dependency raises.

    [if] the state database cannot answer [then] GET /health/live returns 200 alive, [else stop].
    """
    client.app.dependency_overrides[get_read_state] = _boom
    try:
        r = client.get(LIVE_PATH)
    finally:
        client.app.dependency_overrides.pop(get_read_state, None)
    assert r.status_code == 200
    assert r.json()["status"] == "alive"


def test_liveness_ignores_failing_optional_probes(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Liveness never calls the cloud lock or Syncthing probes, so their failure cannot fail it.

    [if] the optional cloud probes raise [then] GET /health/live returns 200 alive, [else stop].
    """
    calls: list[str] = []

    def _failing_probe() -> object:
        """Record the call, then fail like a dead cloud service."""
        calls.append("probe")
        raise RuntimeError("cloud down")

    monkeypatch.setattr(client.app.state, "lock_status_fn", _failing_probe)
    monkeypatch.setattr(client.app.state, "syncthing_status_fn", _failing_probe)
    monkeypatch.setattr(InMemoryBackend, "stats", lambda self: _boom())
    r = client.get(LIVE_PATH)
    assert r.status_code == 200
    assert r.json()["status"] == "alive"
    assert calls == []


def test_liveness_body_is_minimal_and_non_secret(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The liveness body is exactly status and version, with no path or env value.

    [if] the liveness body is read [then] it has only status and version keys, [else stop].
    """
    monkeypatch.setenv("OPENDJ_TEST_SECRET_VALUE", "do-not-leak-12937")
    r = client.get(LIVE_PATH)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"status", "version"}
    text = r.text
    assert "do-not-leak-12937" not in text
    assert "state.db" not in text
    home = os.environ.get("HOME")
    if home:
        assert home not in text
