"""Pairing-capture route tests (PAIR-02): sync snapshots + alignments.

Self-contained: every functional test builds its OWN tmp state.db so
nothing here needs the repo's live state.db. The one live-DB smoke at the
bottom skips cleanly when data/state/state.db is absent.

[if] a POST pairs the same track twice [then] 422 PAIRING_CAPTURE_INVALID, no persist, [else stop].

Regression one-liners:
  - if POST /sync-snapshots does not persist and return a captured row then broken
  - if GET /sync-snapshots does not filter by stable_a/stable_b then broken
  - if a same-track snapshot does not 422 PAIRING_CAPTURE_INVALID then broken
  - if an invalid master_side/sync_mode enum does not 422 then broken
  - if POST /alignments does not persist and return a captured row then broken
  - if GET /alignments does not match either side of stable_a/stable_b then broken
  - if a same-track alignment does not 422 PAIRING_CAPTURE_INVALID then broken
  - if missing state.db does not 503 PAIRING_CAPTURE_DB_UNAVAILABLE then broken
  - if pre-migration db (no capture tables) does not list [] then broken
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.routes import pairing_capture as pairing_capture_routes
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = pytest.mark.requirement("PAIR-02")

_SNAPSHOT_BODY: dict = {
    "stable_a": "track-a",
    "stable_b": "track-b",
    "master_side": "a",
    "sync_mode": "beat",
    "a_tempo_ratio": 1.0,
    "b_tempo_ratio": 0.99,
    "a_position_ms": 123.0,
    "b_position_ms": 456.0,
    "a_position_beat_n": 17,
}

_ALIGNMENT_BODY: dict = {
    "stable_a": "track-a",
    "stable_b": "track-b",
    "anchor_a_kind": "hotcue",
    "anchor_b_kind": "ms",
    "anchor_a_ms": 123.0,
    "anchor_b_ms": 456.0,
    "anchor_a_slot": "A",
}


@pytest.fixture
def state_db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path, apply_schema=True)
    conn.close()
    return path


def _make_client(db_path: Path) -> TestClient:
    app = create_app(
        backend=SqliteBackend(db_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(db_path),
        mount_frontend=False,
    )
    # app.py is a hotspot; tests wire the router exactly the way the
    # integrator will (see tests/test_smartlists_route.py precedent).
    app.include_router(pairing_capture_routes.router, prefix="/api/v1")
    return TestClient(app)


@pytest.fixture
def client(state_db_path: Path) -> Iterator[TestClient]:
    with _make_client(state_db_path) as c:
        yield c


# ----------------------------------------------------------- sync snapshots


def test_list_sync_snapshots_empty_pre_migration_db(client):
    r = client.get("/api/v1/pairings/sync-snapshots")
    assert r.status_code == 200
    assert r.json() == []


def test_create_sync_snapshot_persists_and_returns_captured_row(client):
    r = client.post("/api/v1/pairings/sync-snapshots", json=_SNAPSHOT_BODY)
    assert r.status_code == 201
    body = r.json()
    assert body["stable_a"] == "track-a"
    assert body["stable_b"] == "track-b"
    assert body["master_side"] == "a"
    assert body["a_position_beat_n"] == 17
    assert body["id"]
    assert body["captured_at"]


def test_list_sync_snapshots_filters_by_stable_a(client):
    client.post("/api/v1/pairings/sync-snapshots", json=_SNAPSHOT_BODY)
    other = dict(_SNAPSHOT_BODY, stable_a="track-c", stable_b="track-d")
    client.post("/api/v1/pairings/sync-snapshots", json=other)

    r = client.get("/api/v1/pairings/sync-snapshots", params={"stable_a": "track-a"})
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["stable_a"] == "track-a"


def test_create_sync_snapshot_rejects_same_track(client):
    body = dict(_SNAPSHOT_BODY, stable_a="same", stable_b="same")
    r = client.post("/api/v1/pairings/sync-snapshots", json=body)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "PAIRING_CAPTURE_INVALID"


def test_create_sync_snapshot_rejects_invalid_master_side(client):
    body = dict(_SNAPSHOT_BODY, master_side="middle")
    r = client.post("/api/v1/pairings/sync-snapshots", json=body)
    assert r.status_code == 422


def test_create_sync_snapshot_rejects_negative_position(client):
    body = dict(_SNAPSHOT_BODY, a_position_ms=-1.0)
    r = client.post("/api/v1/pairings/sync-snapshots", json=body)
    assert r.status_code == 422


# ----------------------------------------------------------- alignments


def test_list_alignments_empty_pre_migration_db(client):
    r = client.get("/api/v1/pairings/alignments")
    assert r.status_code == 200
    assert r.json() == []


def test_create_alignment_persists_and_returns_captured_row(client):
    r = client.post("/api/v1/pairings/alignments", json=_ALIGNMENT_BODY)
    assert r.status_code == 201
    body = r.json()
    assert body["stable_a"] == "track-a"
    assert body["anchor_a_kind"] == "hotcue"
    assert body["anchor_a_slot"] == "A"
    assert body["id"]
    assert body["created_at"]


def test_list_alignments_matches_either_side(client):
    client.post("/api/v1/pairings/alignments", json=_ALIGNMENT_BODY)

    by_a = client.get("/api/v1/pairings/alignments", params={"stable_a": "track-a"})
    by_b = client.get("/api/v1/pairings/alignments", params={"stable_a": "track-b"})
    assert by_a.status_code == 200
    assert by_b.status_code == 200
    assert len(by_a.json()) == 1
    assert len(by_b.json()) == 1
    assert by_a.json()[0]["id"] == by_b.json()[0]["id"]


def test_create_alignment_rejects_same_track(client):
    body = dict(_ALIGNMENT_BODY, stable_a="same", stable_b="same")
    r = client.post("/api/v1/pairings/alignments", json=body)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "PAIRING_CAPTURE_INVALID"


def test_create_alignment_rejects_invalid_anchor_kind(client):
    body = dict(_ALIGNMENT_BODY, anchor_a_kind="beam")
    r = client.post("/api/v1/pairings/alignments", json=body)
    assert r.status_code == 422


# ----------------------------------------------------------- db-unavailable


def test_list_sync_snapshots_503_when_db_missing(tmp_path):
    missing = tmp_path / "nope" / "state.db"
    with _make_client(missing) as c:
        r = c.get("/api/v1/pairings/sync-snapshots")
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "PAIRING_CAPTURE_DB_UNAVAILABLE"


def test_create_sync_snapshot_503_when_db_missing(tmp_path):
    missing = tmp_path / "nope" / "state.db"
    with _make_client(missing) as c:
        r = c.post("/api/v1/pairings/sync-snapshots", json=_SNAPSHOT_BODY)
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "PAIRING_CAPTURE_DB_UNAVAILABLE"


def test_live_state_db_smoke():
    """Read-only smoke against the repo's real state.db; skips without it."""
    from apps.shared.paths import STATE_DB

    if not Path(STATE_DB).is_file():
        pytest.skip(f"no live state.db at {STATE_DB}")
    app = create_app(
        backend=SqliteBackend(STATE_DB),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(STATE_DB),
        mount_frontend=False,
    )
    app.include_router(pairing_capture_routes.router, prefix="/api/v1")
    with TestClient(app) as c:
        r = c.get("/api/v1/pairings/sync-snapshots")
    assert r.status_code == 200
    assert isinstance(r.json(), list)
