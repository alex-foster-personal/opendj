"""CloudSync config router tests (LANE config-ui, specs/cloudsync-spec.md D5).

Self-contained: every test builds its OWN tmp state.db (real v6 migrations)
so nothing here touches the repo's live state.db or another lane's fixtures.
``app.py`` is a hotspot owned by the sync-engine lane, so -- like
``tests/test_smartlists_route.py`` -- these tests wire
``app.include_router(cloudsync_routes.router, ...)`` directly onto a bare
``create_app()`` instead of assuming the include line has landed.

Regression one-liners:
  - if GET /cloudsync/machines does not self-register this process's machine then broken
  - if GET /cloudsync/machines is not idempotent (no duplicate rows on repeat calls) then broken
  - if PUT /cloudsync/policies does not round-trip via GET /cloudsync/policies then broken
  - if PUT /cloudsync/policies for an unknown machine_id does not 404 MACHINE_NOT_FOUND then broken
  - if PUT /cloudsync/playlist-pins does not join playlist_name on readback then broken
  - if PUT /cloudsync/playlist-pins for an unknown playlist_id does not 404
    PLAYLIST_NOT_FOUND then broken
  - if GET /cloudsync/overview miscounts pinned vs unhydrated-pinned tracks then broken
  - if a missing state.db does not 503 CLOUDSYNC_DB_UNAVAILABLE then broken
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.routes import cloudsync as cloudsync_routes
from apps.webui.server.sqlite_backend import SqliteBackend

_BASE = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _insert_track(conn: sqlite3.Connection, stable_id: str, title: str) -> None:
    now = _iso(_BASE)
    conn.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, artists_json, "
        "album, isrc, duration_ms, file_path, content_hash, created_at, "
        "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (stable_id, "inferred", title, json.dumps(["Test Artist"]), None,
         None, 300000, None, None, now, now),
    )


def _insert_playlist(
    conn: sqlite3.Connection, playlist_id: str, name: str, stable_ids: list[str],
) -> None:
    now = _iso(_BASE)
    conn.execute(
        "INSERT INTO playlists (playlist_id, name, vendor, vendor_pl_id, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?)",
        (playlist_id, name, "rekordbox", playlist_id, now, now),
    )
    for i, sid in enumerate(stable_ids):
        conn.execute(
            "INSERT INTO playlist_memberships (playlist_id, stable_id, position) "
            "VALUES (?,?,?)",
            (playlist_id, sid, i),
        )


def _insert_track_location(
    conn: sqlite3.Connection, stable_id: str, *, available: bool,
) -> None:
    now = _iso(_BASE)
    conn.execute(
        "INSERT INTO track_locations (stable_id, kind, file_path, available, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?)",
        (stable_id, "local", f"/tmp/{stable_id}.flac", 1 if available else 0,
         now, now),
    )


def _seed_state_db(path: Path) -> None:
    """Real v6 schema + 3 tracks + 1 playlist + mixed hydration state."""
    conn = state_db.open_rw(path, apply_schema=True)
    try:
        _insert_track(conn, "cs-track-001", "Midnight Drive")
        _insert_track(conn, "cs-track-002", "Oxide")
        _insert_track(conn, "cs-track-003", "Gulf")
        _insert_playlist(
            conn, "cs-pl-001", "Gig Crate",
            ["cs-track-001", "cs-track-002", "cs-track-003"],
        )
        # track-001: hydrated local copy. track-002: local row but NOT
        # available (e.g. probed and missing). track-003: no location row
        # at all. Both 002 and 003 should count as unhydrated-when-pinned.
        _insert_track_location(conn, "cs-track-001", available=True)
        _insert_track_location(conn, "cs-track-002", available=False)
    finally:
        conn.close()


@pytest.fixture
def state_db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state" / "state.db"
    _seed_state_db(path)
    return path


def _make_client(db_path: Path, data_dir: Path) -> TestClient:
    app = create_app(
        backend=SqliteBackend(db_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(db_path),
        mount_frontend=False,
    )
    app.state.data_dir = data_dir
    # app.py is a hotspot owned by the sync-engine lane; tests wire the
    # router exactly the way the integrator will.
    app.include_router(cloudsync_routes.router, prefix="/api/v1")
    return TestClient(app)


@pytest.fixture
def client(state_db_path: Path, tmp_path: Path) -> Iterator[TestClient]:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    with _make_client(state_db_path, data_dir) as c:
        yield c


# ----------------------------------------------------------- machines

def test_list_machines_self_registers(client: TestClient):
    r = client.get("/api/v1/cloudsync/machines")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    machine = rows[0]
    assert machine["name"]
    assert machine["platform"] in ("macos", "windows", "linux")
    assert machine["is_hub"] is False
    assert len(machine["machine_id"]) == 32


def test_list_machines_is_idempotent(client: TestClient):
    first = client.get("/api/v1/cloudsync/machines").json()
    second = client.get("/api/v1/cloudsync/machines").json()
    assert len(second) == 1
    assert first[0]["machine_id"] == second[0]["machine_id"]
    assert first[0]["first_seen"] == second[0]["first_seen"]


# ----------------------------------------------------------- policies

def _registered_machine_id(client: TestClient) -> str:
    return client.get("/api/v1/cloudsync/machines").json()[0]["machine_id"]


def test_put_policy_round_trips_via_get(client: TestClient):
    machine_id = _registered_machine_id(client)
    put = client.put("/api/v1/cloudsync/policies", json={
        "machine_id": machine_id, "asset_kind": "stem_bundle",
        "mode": "cached", "cache_budget_mb": 2048,
    })
    assert put.status_code == 200
    body = put.json()
    assert body["mode"] == "cached"
    assert body["cache_budget_mb"] == 2048
    assert body["origin_device_id"] == machine_id
    assert body["updated_at"]

    listed = client.get(
        "/api/v1/cloudsync/policies", params={"machine_id": machine_id}
    ).json()
    assert len(listed) == 1
    assert listed[0]["asset_kind"] == "stem_bundle"
    assert listed[0]["mode"] == "cached"


def test_put_policy_upsert_overwrites_mode(client: TestClient):
    machine_id = _registered_machine_id(client)
    for mode in ("stream", "pinned"):
        client.put("/api/v1/cloudsync/policies", json={
            "machine_id": machine_id, "asset_kind": "anlz_cache", "mode": mode,
        })
    listed = client.get(
        "/api/v1/cloudsync/policies", params={"machine_id": machine_id}
    ).json()
    assert len(listed) == 1
    assert listed[0]["mode"] == "pinned"


def test_put_policy_unknown_machine_404s(client: TestClient):
    r = client.put("/api/v1/cloudsync/policies", json={
        "machine_id": "does-not-exist", "asset_kind": "audio", "mode": "pinned",
    })
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "MACHINE_NOT_FOUND"


def test_put_policy_invalid_mode_422s(client: TestClient):
    machine_id = _registered_machine_id(client)
    r = client.put("/api/v1/cloudsync/policies", json={
        "machine_id": machine_id, "asset_kind": "audio", "mode": "not-a-mode",
    })
    assert r.status_code == 422


# ----------------------------------------------------------- playlist pins

def test_put_playlist_pin_round_trips_with_name(client: TestClient):
    machine_id = _registered_machine_id(client)
    put = client.put("/api/v1/cloudsync/playlist-pins", json={
        "machine_id": machine_id, "playlist_id": "cs-pl-001", "mode": "pinned",
    })
    assert put.status_code == 200
    body = put.json()
    assert body["playlist_name"] == "Gig Crate"
    assert body["mode"] == "pinned"

    listed = client.get(
        "/api/v1/cloudsync/playlist-pins", params={"machine_id": machine_id}
    ).json()
    assert len(listed) == 1
    assert listed[0]["playlist_name"] == "Gig Crate"


def test_put_playlist_pin_unknown_playlist_404s(client: TestClient):
    machine_id = _registered_machine_id(client)
    r = client.put("/api/v1/cloudsync/playlist-pins", json={
        "machine_id": machine_id, "playlist_id": "no-such-playlist", "mode": "pinned",
    })
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "PLAYLIST_NOT_FOUND"


def test_put_playlist_pin_unknown_machine_404s(client: TestClient):
    r = client.put("/api/v1/cloudsync/playlist-pins", json={
        "machine_id": "does-not-exist", "playlist_id": "cs-pl-001", "mode": "pinned",
    })
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "MACHINE_NOT_FOUND"


# ----------------------------------------------------------- overview

def test_overview_counts_pinned_and_unhydrated(client: TestClient):
    machine_id = _registered_machine_id(client)
    client.put("/api/v1/cloudsync/playlist-pins", json={
        "machine_id": machine_id, "playlist_id": "cs-pl-001", "mode": "pinned",
    })

    r = client.get("/api/v1/cloudsync/overview")
    assert r.status_code == 200
    body = r.json()
    assert body["total_tracks"] == 3
    assert len(body["machines"]) == 1
    m = body["machines"][0]
    assert m["machine_id"] == machine_id
    assert m["pinned_tracks"] == 3
    assert m["cached_tracks"] == 0
    assert m["stream_tracks"] == 0
    # track-001 is available locally; track-002 (unavailable row) and
    # track-003 (no row) are not.
    assert m["unhydrated_pinned_count"] == 2
    assert m["last_sync_at"] is None


def test_overview_zero_machines_when_none_registered(state_db_path: Path, tmp_path: Path):
    # No GET /machines call yet in this test -- nothing self-registered.
    data_dir = tmp_path / "data2"
    data_dir.mkdir(parents=True, exist_ok=True)
    with _make_client(state_db_path, data_dir) as c:
        r = c.get("/api/v1/cloudsync/overview")
    assert r.status_code == 200
    body = r.json()
    assert body["total_tracks"] == 3
    assert body["machines"] == []


# ----------------------------------------------------------- unavailable db

def test_missing_db_503s(tmp_path: Path):
    missing = tmp_path / "nope" / "state.db"
    data_dir = tmp_path / "data3"
    data_dir.mkdir(parents=True, exist_ok=True)
    with _make_client(missing, data_dir) as c:
        r = c.get("/api/v1/cloudsync/machines")
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "CLOUDSYNC_DB_UNAVAILABLE"
