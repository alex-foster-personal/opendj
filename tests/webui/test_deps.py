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
