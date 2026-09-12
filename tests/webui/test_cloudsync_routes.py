"""CloudSync config router tests (LANE config-ui, specs/cloudsync-spec.md D5).

Self-contained: every test builds its OWN tmp state.db (real v6 migrations)
so nothing here touches the repo's live state.db or another lane's fixtures.
Cloudsync routes mount through ``app_wiring`` on ``create_app()``; these
tests only need a real ``SqliteBackend`` and ``state_db_path``.

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
  - if a PUT policy / playlist-pin does not append to local_changelog then broken
  - if a UI policy edit does not reach the hub, or the sync after it raises
    SyncDigestMismatch, then broken (round 2 finding N1a)
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state import sync_stamp
from apps.sync_hub import client as sync_client
from apps.sync_hub import service as sync_service
from apps.webui.server.app import create_app
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


def _make_client(db_path: Path) -> TestClient:
    """A webui app serving ``db_path``.

    No ``app.state.data_dir``: since round 3 the cloudsync router takes its
    machine identity from the CONNECTION (``<data-dir>/state/state.db``), so
    setting a second data dir here would describe a configuration that cannot
    exist -- one host, two ``machine_id`` values, and ``machines.name`` is
    UNIQUE.
    """
    app = create_app(
        backend=SqliteBackend(db_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(db_path),
        mount_frontend=False,
    )
    return TestClient(app)


@pytest.fixture
def client(state_db_path: Path) -> Iterator[TestClient]:
    with _make_client(state_db_path) as c:
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


def test_put_policy_invalid_asset_kind_422s(client: TestClient):
    """The AssetKind Literal is the HTTP-level vocabulary gate, before SQLite's CHECK."""
    machine_id = _registered_machine_id(client)
    r = client.put("/api/v1/cloudsync/policies", json={
        "machine_id": machine_id, "asset_kind": "waveform_png", "mode": "pinned",
    })
    assert r.status_code == 422


@pytest.mark.parametrize("asset_kind", ("lyrics_cache", "karaoke_words"))
def test_put_policy_accepts_the_v9_asset_kinds(client: TestClient, asset_kind: str):
    """Schema _V10 widened the sync_policies CHECK; the route must accept both new kinds."""
    machine_id = _registered_machine_id(client)
    put = client.put("/api/v1/cloudsync/policies", json={
        "machine_id": machine_id, "asset_kind": asset_kind, "mode": "pinned",
    })
    assert put.status_code == 200, put.text
    assert put.json()["asset_kind"] == asset_kind
    listed = client.get(
        "/api/v1/cloudsync/policies", params={"machine_id": machine_id}
    ).json()
    assert [row["asset_kind"] for row in listed] == [asset_kind]


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

def test_overview_counts_pinned_and_unhydrated(client: TestClient, state_db_path: Path):
    machine_id = _registered_machine_id(client)
    # The seed rows carry no machine_id. A writable open claims them for this
    # machine (open_rw's post-migration backfill), the way production does,
    # so they count as THIS machine's copies.
    claim = state_db.open_rw(state_db_path)
    try:
        claimed = claim.execute(
            "SELECT COUNT(*) FROM track_locations WHERE machine_id = ?", (machine_id,)
        ).fetchone()[0]
    finally:
        claim.close()
    assert claimed == 2
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


def test_overview_zero_machines_when_none_registered(state_db_path: Path):
    # No GET /machines call yet in this test -- nothing self-registered.
    with _make_client(state_db_path) as c:
        r = c.get("/api/v1/cloudsync/overview")
    assert r.status_code == 200
    body = r.json()
    assert body["total_tracks"] == 3
    assert body["machines"] == []


# ----------------------------------------------------------- unavailable db

def test_missing_db_503s(tmp_path: Path):
    missing = tmp_path / "nope" / "state.db"
    with _make_client(missing) as c:
        r = c.get("/api/v1/cloudsync/machines")
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "CLOUDSYNC_DB_UNAVAILABLE"


# ------------------------------------------- N1a: the config UI must sync
#
# Round 2 finding N1a, reproduced as a permanent regression. Both PUT
# endpoints stamped their row correctly and skipped ``local_changelog``, so
# the write was never offered to the hub and every later sync failed its
# digest compare on that table. The probe that found it:
#
#   [observed] b1 local_changelog sync_policies entries after the UI write: 0
#   [observed] b1 sync 0: ... tables ['sync_policies'] differ
#   [observed] b1 sync 1: ... tables ['sync_policies'] differ
#   [observed] b1 hub sync_policies: []
#
# Changing one policy in the CloudSync config UI stopped that machine syncing
# anything at all, permanently.


class _HubTransport:
    """A :class:`apps.sync_hub.client.HubTransport` backed by ``TestClient``.

    Not a mock of the hub: it drives the real router through the real ASGI
    stack. It exists only because ``TestClient`` is not a URL.
    """

    def __init__(self, http: TestClient) -> None:
        self._http = http

    def _decoded(self, response: Any, label: str) -> dict[str, Any]:
        if response.status_code >= 400:
            raise sync_client.SyncTransportError(
                f"{label} -> HTTP {response.status_code}: {response.text}"
            )
        return dict(response.json())

    def post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._decoded(self._http.post(path, json=dict(payload)), f"POST {path}")

    def get(self, path: str, params: Mapping[str, str]) -> dict[str, Any]:
        return self._decoded(self._http.get(path, params=dict(params)), f"GET {path}")


@pytest.fixture
def hub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_HubTransport]:
    monkeypatch.delenv("MDT_IS_HUB", raising=False)
    hub_dir = tmp_path / "hub"
    app = FastAPI()
    app.state.state_db_path = str(sync_client.state_db_path(hub_dir))
    app.state.sync_hub_data_dir = str(hub_dir)
    app.state.sync_hub_machine_name = "cloudsync-routes-hub"
    app.include_router(sync_service.router, prefix="/api/v1")
    with TestClient(app) as http:
        yield _HubTransport(http)


def _spoke_data_dir(state_db_path: Path) -> Path:
    """``<data-dir>/state/state.db`` is the layout; the data dir is two up."""
    return state_db_path.parent.parent


def _hub_rows(tmp_path: Path, sql: str) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(str(sync_client.state_db_path(tmp_path / "hub")))
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _changelog_entries(db_path: Path, table: str) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT row_pk, updated_at, origin_device_id FROM local_changelog "
            "WHERE table_name = ? ORDER BY seq",
            (table,),
        ).fetchall()
    finally:
        conn.close()


def test_put_policy_appends_to_the_local_changelog(
    client: TestClient, state_db_path: Path,
):
    """The direct N1a assertion, with no hub involved."""
    machine_id = _registered_machine_id(client)
    assert _changelog_entries(state_db_path, "sync_policies") == []

    client.put("/api/v1/cloudsync/policies", json={
        "machine_id": machine_id, "asset_kind": "audio", "mode": "pinned",
    })

    entries = _changelog_entries(state_db_path, "sync_policies")
    assert len(entries) == 1, (
        "a sync_policies write with no local_changelog entry is never offered "
        "to the hub, and the machine then fails its digest compare forever"
    )
    row_pk, updated_at, origin = entries[0]
    assert json.loads(row_pk) == [machine_id, "audio"]
    assert sync_stamp.to_canonical(updated_at) == updated_at
    assert origin == machine_id

    stored = sqlite3.connect(str(state_db_path)).execute(
        "SELECT updated_at, origin_device_id FROM sync_policies"
    ).fetchone()
    assert stored == (updated_at, origin), (
        "the changelog must name the stamp the row actually carries"
    )


def test_put_playlist_pin_appends_to_the_local_changelog(
    client: TestClient, state_db_path: Path,
):
    machine_id = _registered_machine_id(client)
    client.put("/api/v1/cloudsync/playlist-pins", json={
        "machine_id": machine_id, "playlist_id": "cs-pl-001", "mode": "pinned",
    })
    entries = _changelog_entries(state_db_path, "playlist_pins")
    assert len(entries) == 1
    assert json.loads(entries[0][0]) == [machine_id, "cs-pl-001"]


def test_a_ui_policy_edit_reaches_the_hub_and_the_digest_converges(
    client: TestClient, state_db_path: Path, hub: _HubTransport, tmp_path: Path,
):
    """The b1 probe end to end: UI write, sync, converge.

    Before the fix this raised ``SyncDigestMismatch`` on every attempt and the
    hub's ``sync_policies`` stayed empty. The first sync here is what makes it
    a real reproduction rather than an accident of the unfenced full offer:
    it establishes the push watermark, so the second sync can only see the
    policy row through ``local_changelog``.
    """
    machine_id = _registered_machine_id(client)
    data_dir = _spoke_data_dir(state_db_path)

    sync_client.run_sync(data_dir, "http://hub.invalid", transport=hub)

    put = client.put("/api/v1/cloudsync/policies", json={
        "machine_id": machine_id, "asset_kind": "stem_bundle",
        "mode": "cached", "cache_budget_mb": 4096,
    })
    assert put.status_code == 200

    # Raises SyncDigestMismatch if the two sides still disagree afterwards.
    result = sync_client.run_sync(data_dir, "http://hub.invalid", transport=hub)
    assert result.pushed >= 1

    assert _hub_rows(
        tmp_path,
        "SELECT machine_id, asset_kind, mode, cache_budget_mb "
        "FROM sync_policies",
    ) == [(machine_id, "stem_bundle", "cached", 4096)]

    # And a third sync is a clean no-op, not a permanent re-offer.
    settled = sync_client.run_sync(data_dir, "http://hub.invalid", transport=hub)
    assert settled.pushed == 0


def test_a_ui_playlist_pin_reaches_the_hub_and_the_digest_converges(
    client: TestClient, state_db_path: Path, hub: _HubTransport, tmp_path: Path,
):
    machine_id = _registered_machine_id(client)
    data_dir = _spoke_data_dir(state_db_path)

    sync_client.run_sync(data_dir, "http://hub.invalid", transport=hub)

    put = client.put("/api/v1/cloudsync/playlist-pins", json={
        "machine_id": machine_id, "playlist_id": "cs-pl-001", "mode": "pinned",
    })
    assert put.status_code == 200

    sync_client.run_sync(data_dir, "http://hub.invalid", transport=hub)

    assert _hub_rows(
        tmp_path, "SELECT machine_id, playlist_id, mode FROM playlist_pins",
    ) == [(machine_id, "cs-pl-001", "pinned")]


def test_the_router_and_the_sync_client_agree_on_this_machine(
    client: TestClient, state_db_path: Path,
):
    """One host, one identity.

    ``machines.name`` is UNIQUE, so a router that minted its id from a second
    data dir would register a second row under this hostname and 409. The
    router's origin must be the id every other writer stamps with.
    """
    machine_id = _registered_machine_id(client)
    conn = sqlite3.connect(str(state_db_path))
    try:
        assert sync_stamp.local_machine_id(conn) == machine_id
        assert conn.execute("SELECT COUNT(*) FROM machines").fetchone()[0] == 1
    finally:
        conn.close()

pytestmark = pytest.mark.rb_parity
