"""Pin regression tracking via fixed_in_sha (FB-15, issue #3782)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend


@pytest.fixture
def fb(tmp_path: Path) -> TestClient:
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    app = create_app(
        backend=InMemoryBackend(),
        bind_host="127.0.0.1",
        hostname="test-host",
        mount_frontend=False,
    )
    app.state.data_dir = data_dir
    with TestClient(app) as client:
        yield client


def _pin(client: TestClient) -> dict:
    r = client.post(
        "/api/v1/feedback/comments",
        json={
            "x_pct": 5,
            "y_pct": 5,
            "page": "/performance",
            "text": "regression pin",
            "ui": "chrome-loop",
            "viewport_width": 1280,
            "viewport_height": 800,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.requirement("FB-15")
def test_fix_sets_fixed_in_sha(fb: TestClient) -> None:
    """[if] status becomes fixed [then] fixed_in_sha is stamped, [else stop]."""
    pin = _pin(fb)
    r = fb.patch(
        f"/api/v1/feedback/comments/{pin['id']}",
        json={"status": "fixed", "fixed_in_sha": "abc1234"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fixed_in_sha"] == "abc1234"
    assert body["fixed_at"]


@pytest.mark.requirement("FB-15")
def test_reply_on_merged_reopens_and_lists_regressed(fb: TestClient) -> None:
    """[if] merged pin gets reply [then] regressed query lists it, [else stop]."""
    pin = _pin(fb)
    fb.patch(
        f"/api/v1/feedback/comments/{pin['id']}",
        json={"status": "merged", "fixed_in_sha": "deadbeef"},
    )
    r = fb.post(
        f"/api/v1/feedback/comments/{pin['id']}/replies",
        json={"text": "still broken"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "open"
    assert body["fixed_in_sha"] == "deadbeef"

    regressed = fb.get("/api/v1/feedback/comments", params={"state": "regressed"}).json()
    assert any(c["id"] == pin["id"] for c in regressed["comments"])
