"""Dependency tests: lock-by-peer + read-still-works semantics (CAT-05)."""
from __future__ import annotations

import pytest


@pytest.mark.requirement("CAT-05")
def test_read_endpoint_works_when_peer_holds_lock(locked_client):
    r = locked_client.get("/api/v1/tracks")
    assert r.status_code == 200


@pytest.mark.requirement("CAT-05")
def test_write_endpoint_returns_503_when_peer_holds_lock(locked_client):
    r = locked_client.post(
        "/api/v1/pairings",
        json={"from_stable_id": "a", "to_stable_id": "b"},
    )
    assert r.status_code == 503
    body = r.json()
    assert body["detail"]["error"] == "locked_by_peer"
    assert "other-host" in body["detail"]["message"]


@pytest.mark.requirement("CAT-05")
def test_lock_probe_failures_deny_writes(seed_backend):
    """P11-F01: if the lock probe raises, write endpoints must fail-closed.

    Previously the dependency swallowed the exception and returned None,
    which the write guard treated as "no peer holds the lock" — leaving
    every write endpoint open during a cloud-lock outage.
    """
    from fastapi.testclient import TestClient

    from apps.webui.server.app import create_app

    def boom():
        raise RuntimeError("R2 unreachable")

    app = create_app(
        backend=seed_backend, bind_host="127.0.0.1", hostname="test-host",
        lock_status_fn=boom, syncthing_status_fn=lambda: None,
    )
    with TestClient(app) as c:
        # Reads still work — degraded mode, not full outage.
        assert c.get("/api/v1/tracks").status_code == 200
        # Writes must be denied.
        r = c.post(
            "/api/v1/pairings",
            json={"from_stable_id": "a", "to_stable_id": "b"},
        )
        assert r.status_code == 503
        assert r.json()["detail"]["error"] == "lock_probe_failed"

pytestmark = pytest.mark.rb_parity
