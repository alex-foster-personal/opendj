"""Pairings endpoint tests (CAT-03 + CAT-05)."""
from __future__ import annotations

import pytest

from apps.webui.server.etag import compute_etag


@pytest.mark.requirement("CAT-05")
def test_list_pairings(client):
    r = client.get("/api/v1/pairings")
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 1
    assert items[0]["from_stable_id"] == "track-001"


@pytest.mark.requirement("CAT-05")
def test_list_pairings_filter_by_source(client):
    r = client.get("/api/v1/pairings", params={"source": "manual"})
    assert r.status_code == 200
    assert len(r.json()) == 1
    r2 = client.get("/api/v1/pairings", params={"source": "ai"})
    assert r2.status_code == 200
    assert r2.json() == []


@pytest.mark.requirement("CAT-05")
def test_create_pairing_happy_path(client):
    r = client.post(
        "/api/v1/pairings",
        json={"from_stable_id": "track-003", "to_stable_id": "track-004",
              "direction": "->", "source": "manual",
              "notes": "after a break for water"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["from_stable_id"] == "track-003"
    assert body["notes"] == "after a break for water"


@pytest.mark.requirement("CAT-05")
def test_create_pairing_idempotent_concatenates_notes(client):
    r1 = client.post("/api/v1/pairings", json={
        "from_stable_id": "track-003", "to_stable_id": "track-004",
        "direction": "->", "source": "manual", "notes": "first note",
    })
    assert r1.status_code == 201
    r2 = client.post("/api/v1/pairings", json={
        "from_stable_id": "track-003", "to_stable_id": "track-004",
        "direction": "->", "source": "manual", "notes": "second note",
    })
    assert r2.status_code == 201
    assert r2.json()["pairing_id"] == r1.json()["pairing_id"]
    assert "first note" in r2.json()["notes"]
    assert "second note" in r2.json()["notes"]


@pytest.mark.requirement("CAT-05")
def test_delete_pairing_requires_if_match(client):
    r = client.delete("/api/v1/pairings/p-001")
    assert r.status_code == 428


@pytest.mark.requirement("CAT-05")
def test_delete_pairing_with_stale_etag_409(client):
    r = client.delete("/api/v1/pairings/p-001",
                      headers={"If-Match": '"stale"'})
    assert r.status_code == 409


@pytest.mark.requirement("CAT-05")
def test_delete_pairing_happy_path(client, seed_backend):
    pairing = seed_backend.list_pairings()[0]
    etag = compute_etag(pairing.pairing_id, pairing.updated_at)
    r = client.delete(f"/api/v1/pairings/{pairing.pairing_id}",
                      headers={"If-Match": etag})
    assert r.status_code == 204
    assert seed_backend.list_pairings() == []


@pytest.mark.requirement("CAT-05")
def test_create_pairing_notes_too_long_422(client):
    r = client.post("/api/v1/pairings", json={
        "from_stable_id": "a", "to_stable_id": "b", "notes": "x" * 1001,
    })
    assert r.status_code == 422
