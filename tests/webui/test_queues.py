"""Queues endpoint tests (CAT-05)."""
from __future__ import annotations

import pytest


@pytest.mark.requirement("CAT-05")
def test_get_dedup_queue(client):
    r = client.get("/api/v1/queues/dedup")
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["stable_id"] == "track-003"


@pytest.mark.requirement("CAT-05")
def test_get_bad_beatgrid_empty(client):
    r = client.get("/api/v1/queues/bad_beatgrid")
    assert r.status_code == 200
    assert r.json()["items"] == []


@pytest.mark.requirement("CAT-05")
def test_queue_unknown_kind_404(client):
    r = client.get("/api/v1/queues/not_real")
    assert r.status_code == 404


@pytest.mark.requirement("CAT-05")
def test_queue_marked_unavailable_shows_note(client, seed_backend):
    seed_backend.mark_queue_unavailable(
        "auto_cue", note="Phase 6 auto_cue not merged; queue empty."
    )
    r = client.get("/api/v1/queues/auto_cue")
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert "not merged" in body["note"]

pytestmark = pytest.mark.rb_parity
