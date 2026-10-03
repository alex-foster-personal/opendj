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
def test_create_pairing_persists_the_open_time_snapshot(client):
    snapshot = {
        "version": 1,
        "beat_sync_max": True,
        "decks": [
            {
                "deck_id": 1, "stable_id": "track-003", "title": "First", "position_ms": 12000,
                "timestamp": {"unit": "beats", "value": 25},
                "eq_adjusts": [{"band": "low", "value": 0.2}],
            },
            {"deck_id": 2, "stable_id": "track-004", "title": "Second", "position_ms": 13000,
             "timestamp": {"unit": "beats", "value": 27}, "eq_adjusts": []},
        ],
    }
    response = client.post("/api/v1/pairings", json={
        "from_stable_id": "track-003", "to_stable_id": "track-004", "snapshot": snapshot,
    })
    assert response.status_code == 201
    assert response.json()["snapshot"] == snapshot


@pytest.mark.requirement("CAT-05")
def test_create_pairing_notes_preserves_existing_snapshot(client):
    snapshot = {
        "version": 1, "beat_sync_max": False,
        "decks": [
            {"deck_id": 1, "stable_id": "track-003", "title": "First", "position_ms": 12000,
             "timestamp": {"unit": "time", "value": 12000}, "eq_adjusts": []},
            {"deck_id": 2, "stable_id": "track-004", "title": "Second", "position_ms": 13000,
             "timestamp": {"unit": "time", "value": 13000}, "eq_adjusts": []},
        ],
    }
    initial = client.post("/api/v1/pairings", json={
        "from_stable_id": "track-003", "to_stable_id": "track-004", "snapshot": snapshot,
    })
    assert initial.status_code == 201
    updated = client.post("/api/v1/pairings", json={
        "from_stable_id": "track-003", "to_stable_id": "track-004", "notes": "keep this snapshot",
    })
    assert updated.status_code == 201
    assert updated.json()["snapshot"] == snapshot

    recaptured = client.post("/api/v1/pairings", json={
        "from_stable_id": "track-003", "to_stable_id": "track-004",
        "snapshot": {
            **snapshot,
            "decks": [{**snapshot["decks"][0], "position_ms": 99999}, snapshot["decks"][1]],
        },
    })
    assert recaptured.status_code == 201
    assert recaptured.json()["snapshot"] == snapshot


@pytest.mark.requirement("CAT-05")
def test_create_pairing_notes_and_snapshot_together_keep_the_frozen_snapshot(client):
    """Notes merging must not smuggle a snapshot replacement past write-once.

    The snapshot-only branch already refuses to overwrite a frozen capture.
    A duplicate post carrying BOTH changed notes and a new snapshot has to
    obey the same contract, otherwise the original open-time moment is lost
    whenever the operator retypes a note during a recapture.
    """
    frozen = {
        "version": 1, "beat_sync_max": False,
        "decks": [
            {"deck_id": 1, "stable_id": "track-003", "title": "First", "position_ms": 12000,
             "timestamp": {"unit": "time", "value": 12000}, "eq_adjusts": []},
            {"deck_id": 2, "stable_id": "track-004", "title": "Second", "position_ms": 13000,
             "timestamp": {"unit": "time", "value": 13000}, "eq_adjusts": []},
        ],
    }
    initial = client.post("/api/v1/pairings", json={
        "from_stable_id": "track-003", "to_stable_id": "track-004",
        "notes": "first capture", "snapshot": frozen,
    })
    assert initial.status_code == 201
    assert initial.json()["snapshot"] == frozen

    later = client.post("/api/v1/pairings", json={
        "from_stable_id": "track-003", "to_stable_id": "track-004",
        "notes": "second thought",
        "snapshot": {
            **frozen,
            "decks": [
                {**frozen["decks"][0], "position_ms": 99999,
                 "timestamp": {"unit": "time", "value": 99999},
                 "eq_adjusts": [{"band": "high", "value": 0.9}]},
                frozen["decks"][1],
            ],
        },
    })
    assert later.status_code == 201
    body = later.json()
    assert body["snapshot"] == frozen, "notes merge overwrote the frozen snapshot"
    assert "second thought" in body["notes"]
    assert "first capture" in body["notes"]

    listed = [row for row in client.get("/api/v1/pairings").json()
              if row["from_stable_id"] == "track-003"]
    assert len(listed) == 1
    assert listed[0]["snapshot"] == frozen


@pytest.mark.requirement("CAT-05")
def test_create_pairing_notes_attach_a_snapshot_when_none_was_frozen(client):
    """A pairing created without a snapshot still accepts its first one."""
    snapshot = {
        "version": 1, "beat_sync_max": False,
        "decks": [
            {"deck_id": 1, "stable_id": "track-003", "title": "First", "position_ms": 4000,
             "timestamp": {"unit": "time", "value": 4000}, "eq_adjusts": []},
            {"deck_id": 2, "stable_id": "track-004", "title": "Second", "position_ms": 5000,
             "timestamp": {"unit": "time", "value": 5000}, "eq_adjusts": []},
        ],
    }
    initial = client.post("/api/v1/pairings", json={
        "from_stable_id": "track-003", "to_stable_id": "track-004", "notes": "no snapshot yet",
    })
    assert initial.status_code == 201
    assert initial.json()["snapshot"] is None

    filled = client.post("/api/v1/pairings", json={
        "from_stable_id": "track-003", "to_stable_id": "track-004",
        "notes": "now with state", "snapshot": snapshot,
    })
    assert filled.status_code == 201
    assert filled.json()["snapshot"] == snapshot


@pytest.mark.requirement("CAT-05")
def test_create_pairing_rejects_snapshot_timestamp_units_that_contradict_beat_sync(client):
    snapshot = {
        "version": 1, "beat_sync_max": True,
        "decks": [
            {"deck_id": 1, "stable_id": "track-003", "title": "First", "position_ms": 0,
             "timestamp": {"unit": "time", "value": 0}, "eq_adjusts": []},
            {"deck_id": 2, "stable_id": "track-004", "title": "Second", "position_ms": 0,
             "timestamp": {"unit": "time", "value": 0}, "eq_adjusts": []},
        ],
    }
    response = client.post("/api/v1/pairings", json={
        "from_stable_id": "track-003", "to_stable_id": "track-004", "snapshot": snapshot,
    })
    assert response.status_code == 422


@pytest.mark.requirement("CAT-05")
def test_create_pairing_rejects_snapshot_with_wrong_pair_decks(client):
    snapshot = {
        "version": 1, "beat_sync_max": False,
        "decks": [
            {"deck_id": 1, "stable_id": "wrong-track", "title": "Wrong", "position_ms": 0,
             "timestamp": {"unit": "time", "value": 0}, "eq_adjusts": []},
            {"deck_id": 2, "stable_id": "track-004", "title": "Second", "position_ms": 0,
             "timestamp": {"unit": "time", "value": 0}, "eq_adjusts": []},
        ],
    }
    response = client.post("/api/v1/pairings", json={
        "from_stable_id": "track-003", "to_stable_id": "track-004", "snapshot": snapshot,
    })
    assert response.status_code == 422


@pytest.mark.requirement("CAT-05")
def test_create_pairing_rejects_snapshot_with_duplicate_deck_ids(client):
    snapshot = {
        "version": 1, "beat_sync_max": False,
        "decks": [
            {"deck_id": 1, "stable_id": "track-003", "title": "First", "position_ms": 0,
             "timestamp": {"unit": "time", "value": 0}, "eq_adjusts": []},
            {"deck_id": 1, "stable_id": "track-004", "title": "Second", "position_ms": 0,
             "timestamp": {"unit": "time", "value": 0}, "eq_adjusts": []},
        ],
    }
    response = client.post("/api/v1/pairings", json={
        "from_stable_id": "track-003", "to_stable_id": "track-004", "snapshot": snapshot,
    })
    assert response.status_code == 422


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

pytestmark = pytest.mark.rb_parity


@pytest.mark.requirement("PAIR-04")
def test_create_pairing_rejects_a_self_pair_with_422(client):
    """If a pairing names one track twice then the API answers 422, else stop."""
    r = client.post(
        "/api/v1/pairings",
        json={"from_stable_id": "track-003", "to_stable_id": "track-003"},
    )
    assert r.status_code == 422
    assert all(
        p["to_stable_id"] != p["from_stable_id"]
        for p in client.get("/api/v1/pairings").json()
    )


@pytest.mark.requirement("PAIR-04")
@pytest.mark.parametrize("ends", [("", "track-003"), ("track-003", "")])
def test_create_pairing_rejects_an_empty_endpoint_with_422(client, ends):
    """If a pairing names an empty track id then the API answers 422, else stop."""
    r = client.post(
        "/api/v1/pairings",
        json={"from_stable_id": ends[0], "to_stable_id": ends[1]},
    )
    assert r.status_code == 422
    assert not [
        p for p in client.get("/api/v1/pairings").json()
        if "" in (p["from_stable_id"], p["to_stable_id"])
    ]
