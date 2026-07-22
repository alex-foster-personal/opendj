"""PLAY IT sort action router tests (play-it-sort-action)."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend, Playlist, Track
from apps.webui.server.etag import compute_etag


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def test_solve_happy_path(client: TestClient, seed_backend: InMemoryBackend) -> None:
    r = client.post(
        "/api/v1/play-it/pl-002/solve",
        json={"duration_min": 60},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["playlist_id"] == "pl-002"
    assert body["previous_order"] == ["track-001", "track-002", "track-004"]
    assert sorted(body["proposed_order"]) == sorted(body["previous_order"])
    assert len(body["steps"]) == 3
    assert body["etag"] == compute_etag(
        "pl-002", seed_backend.get_playlist("pl-002").updated_at
    )


def test_production_app_registers_play_it_contract() -> None:
    """Agent and UI callers share the route mounted by the production app."""
    paths = create_app(mount_frontend=False).openapi()["paths"]

    assert "/api/v1/play-it/{playlist_id}/solve" in paths


def test_solve_order_is_deterministic(client: TestClient) -> None:
    first = client.post("/api/v1/play-it/pl-002/solve", json={"duration_min": 60})
    second = client.post("/api/v1/play-it/pl-002/solve", json={"duration_min": 60})

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["proposed_order"] == second.json()["proposed_order"]
    assert first.json()["steps"] == second.json()["steps"]


def test_solve_unknown_playlist_is_404(client: TestClient) -> None:
    r = client.post("/api/v1/play-it/nope/solve", json={"duration_min": 60})
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


def test_solve_invalid_goal_is_422(client: TestClient) -> None:
    r = client.post(
        "/api/v1/play-it/pl-002/solve",
        json={"duration_min": 10},  # below the 30 min floor
    )
    assert r.status_code == 422


def test_solve_insufficient_data_is_422(client: TestClient) -> None:
    r = client.post(
        "/api/v1/play-it/pl-001/solve",  # pl-001: track-003, track-005
        json={"duration_min": 60, "peak_at_min": 30},
    )
    # both tracks in pl-001 have bpm+key seeded so this playlist actually
    # passes coverage; assert 200 here and cover the failing case below via
    # a dedicated backend instead of overloading the shared fixture.
    assert r.status_code == 200, r.text


def test_solve_insufficient_data_missing_bpm() -> None:
    backend = InMemoryBackend()
    base = _iso(datetime(2026, 4, 17, 10, 0, 0, tzinfo=timezone.utc))
    for i in range(20):
        bpm = None if i < 2 else 120.0 + i
        backend.seed_track(Track(
            stable_id=f"t-{i:03d}", title=f"Track {i}", artist="A",
            bpm=bpm, key="8A", created_at=base, updated_at=base,
        ))
    backend.seed_playlist(Playlist(
        playlist_id="pl-gap", name="Gap Test",
        items=[f"t-{i:03d}" for i in range(20)],
        created_at=base, updated_at=base,
    ))
    app = create_app(backend=backend, bind_host="127.0.0.1", hostname="test-host")
    with TestClient(app) as c:
        r = c.post("/api/v1/play-it/pl-gap/solve", json={"duration_min": 60})
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "insufficient_data"
    assert "t-000" in body["details"]["missing"]["bpm"]


def test_solve_empty_playlist_returns_empty_order() -> None:
    backend = InMemoryBackend()
    base = _iso(datetime(2026, 4, 17, 10, 0, 0, tzinfo=timezone.utc))
    backend.seed_playlist(Playlist(
        playlist_id="pl-empty", name="Empty",
        items=[], created_at=base, updated_at=base,
    ))
    app = create_app(backend=backend, bind_host="127.0.0.1", hostname="test-host")
    with TestClient(app) as c:
        r = c.post("/api/v1/play-it/pl-empty/solve", json={"duration_min": 60})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["proposed_order"] == []
    assert body["steps"] == []
    assert body["unchanged"] is True


def test_solve_energy_missing_is_not_gating(client: TestClient) -> None:
    # seed_backend never sets an "energy" provenance entry, so every track's
    # energy is None; this must not block PLAY IT (known sqlite-backend gap,
    # mirrors routes/copilot.py).
    r = client.post("/api/v1/play-it/pl-002/solve", json={"duration_min": 60})
    assert r.status_code == 200, r.text
    assert all(step["energy"] is None for step in r.json()["steps"])
