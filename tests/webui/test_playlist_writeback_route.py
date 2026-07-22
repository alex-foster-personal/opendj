"""HTTP-surface tests for the playlist writeback router (LANE
playlists-router, node ``write-back-rekordbox-djay``). Service-layer logic
is covered in ``test_playlist_writeback_service.py``; these tests only
check routing, status codes, and the request/response shape.

Regression one-liners:
  * if an unknown playlist_id does not 404 then broken
  * if an unreachable vendor does not 503 with WRITEBACK_VENDOR_UNAVAILABLE
    then broken
  * if apply defaults to a live write instead of dry_run=True then broken
  * if apply does not respect the cloud writer lock then broken
"""
from __future__ import annotations

import sqlite3
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend
from apps.webui.server.playlist_writeback import WritebackService
from apps.webui.server.routes.playlist_writeback import get_writeback_service


def _fake_state_conn() -> sqlite3.Connection:
    """In-memory track_vendor_ids covering every seed_backend track, so a
    fake writer resolves the same way a real RB/djay writer would once the
    tracks are ingested."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute(
        "CREATE TABLE track_vendor_ids ("
        "stable_id TEXT, vendor TEXT, vendor_id TEXT, "
        "PRIMARY KEY (stable_id, vendor))"
    )
    rows = [
        (f"track-{i:03d}", "rekordbox", f"rb-{i:03d}") for i in range(1, 6)
    ]
    conn.executemany("INSERT INTO track_vendor_ids VALUES (?, ?, ?)", rows)
    return conn


class _FakeWriter:
    def __init__(self, vendor: str, playlists: dict[str, list[str]] | None = None) -> None:
        self.vendor = vendor
        self.state_conn = _fake_state_conn()
        self.playlists = playlists or {}
        self.calls: list[tuple] = []

    def playlist_exists(self, name: str) -> bool:
        return name in self.playlists

    def read_members(self, name: str) -> list[str]:
        return list(self.playlists[name])

    def create_playlist(self, name: str, track_ids: list[str]) -> None:
        self.calls.append(("create", name, list(track_ids)))
        self.playlists[name] = list(track_ids)

    def apply_diff(self, name: str, added: list[str], removed: list[str]) -> None:
        self.calls.append(("diff", name, list(added), list(removed)))
        current = self.playlists.setdefault(name, [])
        for rid in removed:
            if rid in current:
                current.remove(rid)
        for aid in added:
            if aid not in current:
                current.append(aid)


def _fake_service(rb_playlists: dict | None = None) -> WritebackService:
    rb = _FakeWriter("rekordbox", rb_playlists)

    def factory(vendor: str):
        if vendor == "rekordbox":
            return rb
        return None  # djay unreachable in every test client below

    return WritebackService(writer_factory=factory)


@pytest.fixture
def client(seed_backend: InMemoryBackend) -> Iterator[TestClient]:
    app = create_app(
        backend=seed_backend, bind_host="127.0.0.1", hostname="test-host",
        lock_status_fn=lambda: None,
    )
    app.dependency_overrides[get_writeback_service] = lambda: _fake_service()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


class TestUnknownPlaylist:
    def test_capabilities_404s_for_unknown_playlist(self, client: TestClient) -> None:
        r = client.get("/api/v1/playlists/no-such/writeback/capabilities")
        assert r.status_code == 404

    def test_plan_404s_for_unknown_playlist(self, client: TestClient) -> None:
        r = client.get("/api/v1/playlists/no-such/writeback/plan?vendor=rekordbox")
        assert r.status_code == 404

    def test_apply_404s_for_unknown_playlist(self, client: TestClient) -> None:
        r = client.post("/api/v1/playlists/no-such/writeback/apply", json={"vendor": "rekordbox"})
        assert r.status_code == 404


class TestCapabilities:
    def test_reports_per_vendor_availability(self, client: TestClient) -> None:
        r = client.get("/api/v1/playlists/pl-001/writeback/capabilities")
        assert r.status_code == 200, r.text
        body = r.json()
        by_vendor = {v["vendor"]: v for v in body["vendors"]}
        assert by_vendor["rekordbox"]["available"] is True
        assert by_vendor["djay"]["available"] is False
        assert by_vendor["djay"]["reason"]


class TestPlan:
    def test_plan_reports_diff_for_new_target(self, client: TestClient) -> None:
        # pl-001 seeds items=["track-003", "track-005"], both resolvable
        # via the fake writer's state_conn; no rekordbox target named
        # "Opener Set" exists yet, so the plan is an all-additions diff.
        r = client.get("/api/v1/playlists/pl-001/writeback/plan?vendor=rekordbox")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["target_exists"] is False
        assert set(body["added"]) == {"track-003", "track-005"}
        assert body["removed"] == []
        assert body["unresolved"] == []

    def test_plan_503s_for_unreachable_vendor(self, client: TestClient) -> None:
        r = client.get("/api/v1/playlists/pl-001/writeback/plan?vendor=djay")
        assert r.status_code == 503
        assert r.json()["detail"]["code"] == "WRITEBACK_VENDOR_UNAVAILABLE"

    def test_plan_requires_a_known_vendor_literal(self, client: TestClient) -> None:
        r = client.get("/api/v1/playlists/pl-001/writeback/plan?vendor=spotify")
        assert r.status_code == 422


class TestApply:
    def test_apply_defaults_to_dry_run(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/playlists/pl-001/writeback/apply",
            json={"vendor": "rekordbox"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["dry_run"] is True
        assert body["applied"] is False

    def test_apply_503s_for_unreachable_vendor(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/playlists/pl-001/writeback/apply",
            json={"vendor": "djay", "dry_run": False},
        )
        assert r.status_code == 503

    def test_apply_locked_by_peer_yields_503(self, seed_backend: InMemoryBackend) -> None:
        app = create_app(
            backend=seed_backend, bind_host="127.0.0.1", hostname="test-host",
            lock_status_fn=lambda: {"holder": "other-host", "expires_at": "2099-01-01T00:00:00Z"},
        )
        app.dependency_overrides[get_writeback_service] = lambda: _fake_service()
        with TestClient(app) as c:
            r = c.post(
                "/api/v1/playlists/pl-001/writeback/apply",
                json={"vendor": "rekordbox", "dry_run": False},
            )
        assert r.status_code == 503
