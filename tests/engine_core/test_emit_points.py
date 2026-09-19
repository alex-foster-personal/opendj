"""Emit points: legacy mutation endpoints publish through the events seam.

The hub below is a REAL hub registered through the public ``set_hub()``
surface -- the same call the chassis makes at startup -- not a monkeypatch
of ``publish``. That is the point: if the seam contract changes, these
tests break where the contract broke.

Single-line intent, in the repo's regression style:
  - if a successful mutation does not publish library.changed then clients
    never learn to refetch and the UI silently shows stale rows
  - if the payload kind/ids drift from the agreed vocabulary then every
    consumer's invalidation switch falls through
  - if a REJECTED mutation publishes then clients refetch for a write that
    never landed, which is a lie about the library state
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.shared.events import set_hub
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend
from apps.webui.server.etag import compute_etag
from tests.webui.conftest import current_etag


class RecordingHub:
    """Records every delivered event. Five lines, real EventHub shape."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        self.events.append((topic, payload))


@pytest.fixture
def hub() -> Iterator[RecordingHub]:
    """Register a live hub for the test, then hand the seam back empty."""
    recorder = RecordingHub()
    set_hub(recorder)
    yield recorder
    set_hub(None)


@pytest.fixture
def prefs_client(tmp_path: Path) -> Iterator[TestClient]:
    """ui-prefs writes a file under data_dir, so it needs its own app."""
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    app = create_app(
        backend=InMemoryBackend(), bind_host="127.0.0.1",
        hostname="test-host", mount_frontend=False,
    )
    app.state.data_dir = data_dir
    with TestClient(app) as test_client:
        yield test_client


def test_patch_track_publishes_tracks_invalidation(client, seed_backend, hub) -> None:
    response = client.patch(
        "/api/v1/tracks/track-001", json={"rating": 2},
        headers={"If-Match": current_etag(seed_backend, "track-001")},
    )
    assert response.status_code == 200
    assert hub.events == [
        ("library.changed", {"kind": "tracks", "ids": ["track-001"]})
    ]


def test_bulk_edit_publishes_every_written_id(client, seed_backend, hub) -> None:
    response = client.patch(
        "/api/v1/bulk-edit",
        json={
            "stable_ids": ["track-001", "track-002"],
            "expected_etags": {
                "track-001": current_etag(seed_backend, "track-001"),
                "track-002": current_etag(seed_backend, "track-002"),
            },
            "rating": 1,
        },
    )
    assert response.status_code == 200
    topic, payload = hub.events[0]
    assert topic == "library.changed"
    assert payload["kind"] == "tracks"
    assert sorted(payload["ids"]) == ["track-001", "track-002"]


def test_rejected_bulk_edit_publishes_nothing(client, seed_backend, hub) -> None:
    response = client.patch(
        "/api/v1/bulk-edit",
        json={
            "stable_ids": ["track-001", "track-002"],
            "expected_etags": {
                "track-001": '"stale"',
                "track-002": current_etag(seed_backend, "track-002"),
            },
            "rating": 1,
        },
    )
    assert response.status_code == 409
    assert hub.events == []


def test_mytag_assign_publishes_the_touched_tag_names(client, seed_backend, hub) -> None:
    response = client.post(
        "/api/v1/mytags/assign",
        json={
            "stable_ids": ["track-001"],
            "expected_etags": {"track-001": current_etag(seed_backend, "track-001")},
            "add": ["peak-time"],
            "remove": ["deep-house"],
        },
    )
    assert response.status_code == 200
    assert hub.events == [
        ("library.changed",
         {"kind": "mytags", "ids": ["deep-house", "peak-time"]})
    ]


def test_pairing_create_and_delete_publish_pairings(client, seed_backend, hub) -> None:
    created = client.post("/api/v1/pairings", json={
        "from_stable_id": "track-003", "to_stable_id": "track-004",
        "direction": "->", "source": "manual",
    })
    assert created.status_code == 201
    pairing_id = created.json()["pairing_id"]
    assert hub.events == [
        ("library.changed", {"kind": "pairings", "ids": [pairing_id]})
    ]

    pairing = next(
        p for p in seed_backend.list_pairings() if p.pairing_id == pairing_id
    )
    deleted = client.delete(
        f"/api/v1/pairings/{pairing_id}",
        headers={"If-Match": compute_etag(pairing.pairing_id, pairing.updated_at)},
    )
    assert deleted.status_code == 204
    assert hub.events[1] == (
        "library.changed", {"kind": "pairings", "ids": [pairing_id]}
    )


def test_ui_prefs_put_publishes_ui_prefs(prefs_client, hub) -> None:
    response = prefs_client.put("/api/v1/ui-prefs", json={"theme": "light"})
    assert response.status_code == 200
    assert hub.events == [
        ("library.changed", {"kind": "ui_prefs", "ids": []})
    ]


def test_midi_map_put_and_delete_publish_midi_maps(prefs_client, hub) -> None:
    """kind is 'midi_maps' (plural), matching LIBRARY_KINDS in
    events-bus.ts - the frontend's allowlist drops any kind it does not
    recognize, so a singular/plural mismatch here is a silent no-op there."""
    doc = {
        "schemaVersion": 1,
        "id": "test-device",
        "vendor": "TestCo",
        "model": "TestDevice 1",
        "nameMatch": "TestDevice",
        "bindings": [
            {
                "source": {"ch": 1, "kind": "note", "id": 11},
                "action": {"type": "deck_play_toggle", "deck": 1},
                "provenance": {"tier": "learned", "cite": "learn wizard", "verified": True},
            }
        ],
    }
    put = prefs_client.put("/api/v1/midi/maps/test-device", json=doc)
    assert put.status_code == 200
    assert hub.events == [
        ("library.changed", {"kind": "midi_maps", "ids": ["test-device"]})
    ]

    deleted = prefs_client.delete("/api/v1/midi/maps/test-device")
    assert deleted.status_code == 204
    assert hub.events[1] == (
        "library.changed", {"kind": "midi_maps", "ids": ["test-device"]}
    )


def test_no_hub_registered_is_a_silent_no_op(client, seed_backend) -> None:
    """Legacy boots have no WS server, so publish() delivers to nobody."""
    set_hub(None)
    response = client.patch(
        "/api/v1/tracks/track-002", json={"rating": 4},
        headers={"If-Match": current_etag(seed_backend, "track-002")},
    )
    assert response.status_code == 200
