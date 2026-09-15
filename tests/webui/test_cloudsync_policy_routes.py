"""Policy gate over HTTP: tombstones that sync, the 409 gate on PUT, and the
machine-scoped overview count.

Every test runs the full webui app (routes mounted by ``app_wiring``) over a
real migrated tmp state DB, and the hub tests drive the real sync router
in-process through ``TestClientTransport``.

- [if] a DELETE tombstone does not reach the hub, or the digest diverges [then] fail, [else stop].
- [if] another machine's copy makes a pinned machine read as hydrated [then] fail, [else stop].
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.sync_hub import client as sync_client
from apps.sync_hub import service as sync_service
from tests.cloudsync.enrollment_transport import TestClientTransport
from tests.cloudsync.policy_fixtures import (
    OTHER_ID,
    PLAYLIST_ID,
    STAMP,
    TRACKS,
    add_local_copy,
    add_remote_machine,
    count,
    http_client,
    local_id,
    make_fleet_dir,
)

pytestmark = pytest.mark.requirement("CLOUDSYNC-01")

HUB_URL = "http://hub.invalid"


@pytest.fixture
def spoke_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("MDT_IS_HUB", raising=False)
    return make_fleet_dir(tmp_path / "spoke", with_hub=False)


@pytest.fixture
def hub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClientTransport]:
    monkeypatch.delenv("MDT_IS_HUB", raising=False)
    hub_dir = tmp_path / "hub"
    app = FastAPI()
    app.state.state_db_path = str(sync_client.state_db_path(hub_dir))
    app.state.sync_hub_data_dir = str(hub_dir)
    app.state.sync_hub_machine_name = "policy-routes-hub"
    app.include_router(sync_service.router, prefix="/api/v1")
    with TestClient(app) as http:
        yield TestClientTransport(http)


def _hub_rows(tmp_path: Path, sql: str) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(sync_client.state_db_path(tmp_path / "hub"))
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


# ----- tombstones reach the hub ----------------------------------------------------


def test_policy_delete_tombstone_reaches_the_hub_and_the_digest_converges(
    spoke_dir: Path, hub: TestClientTransport, tmp_path: Path
) -> None:
    """if a DELETE policy tombstone does not sync or the digest diverges then broken"""
    me = local_id(spoke_dir)
    sync_client.run_sync(spoke_dir, HUB_URL, transport=hub)
    with http_client(spoke_dir) as http:
        put = http.put(
            "/api/v1/cloudsync/policies",
            json={"machine_id": me, "asset_kind": "audio", "mode": "pinned"},
        )
        assert put.status_code == 200, put.text
        sync_client.run_sync(spoke_dir, HUB_URL, transport=hub)
        assert _hub_rows(tmp_path, "SELECT deleted_at FROM sync_policies") == [(None,)]

        deleted = http.delete(f"/api/v1/cloudsync/policies/{me}/audio")
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["written"] is True
        assert http.get("/api/v1/cloudsync/policies").json() == []

    # Raises SyncDigestMismatch if the two sides disagree after the tombstone.
    result = sync_client.run_sync(spoke_dir, HUB_URL, transport=hub)
    assert result.pushed >= 1
    assert _hub_rows(
        tmp_path, "SELECT machine_id, asset_kind, deleted_at IS NOT NULL FROM sync_policies"
    ) == [(me, "audio", 1)]
    assert sync_client.run_sync(spoke_dir, HUB_URL, transport=hub).pushed == 0


def test_unpin_via_apply_tombstone_reaches_the_hub(
    spoke_dir: Path, hub: TestClientTransport, tmp_path: Path
) -> None:
    """if unpinning through POST /apply does not tombstone the pin on the hub then broken"""
    me = local_id(spoke_dir)
    sync_client.run_sync(spoke_dir, HUB_URL, transport=hub)
    with http_client(spoke_dir) as http:
        assert (
            http.put(
                "/api/v1/cloudsync/playlist-pins",
                json={"machine_id": me, "playlist_id": PLAYLIST_ID, "mode": "pinned"},
            ).status_code
            == 200
        )
        sync_client.run_sync(spoke_dir, HUB_URL, transport=hub)
        unpin = {"removed_pins": [{"machine_id": me, "playlist_id": PLAYLIST_ID}]}
        applied = http.post(
            "/api/v1/cloudsync/policies/apply", json={"changes": unpin, "dry_run": False}
        )
        assert applied.status_code == 200, applied.text
        assert applied.json()["written"] is True
    sync_client.run_sync(spoke_dir, HUB_URL, transport=hub)
    assert _hub_rows(tmp_path, "SELECT playlist_id, deleted_at IS NOT NULL FROM playlist_pins") == [
        (PLAYLIST_ID, 1)
    ]


def test_delete_of_a_missing_cell_is_404_and_writes_nothing(spoke_dir: Path) -> None:
    """if unsetting a cell that has no live row succeeds or writes a changelog entry then broken"""
    me = local_id(spoke_dir)
    before = count(spoke_dir, "SELECT COUNT(*) FROM local_changelog")
    with http_client(spoke_dir) as http:
        response = http.delete(f"/api/v1/cloudsync/policies/{me}/audio")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "POLICY_NOT_FOUND"
    assert count(spoke_dir, "SELECT COUNT(*) FROM local_changelog") == before


# ----- the PUT gate ------------------------------------------------------------------


def test_put_that_introduces_an_error_is_409_and_writes_nothing(spoke_dir: Path) -> None:
    """if a PUT whose budget breaks cache_budget is accepted or writes a row then broken"""
    me = local_id(spoke_dir)
    with http_client(spoke_dir) as http:
        refused = http.put(
            "/api/v1/cloudsync/policies",
            json={"machine_id": me, "asset_kind": "audio", "mode": "stream", "cache_budget_mb": 9},
        )
        accepted = http.put(
            "/api/v1/cloudsync/policies",
            json={"machine_id": me, "asset_kind": "audio", "mode": "cached", "cache_budget_mb": 9},
        )
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "POLICY_VIOLATION"
    assert accepted.status_code == 200, "the control: the same cell with a legal mode is accepted"
    assert count(spoke_dir, "SELECT COUNT(*) FROM sync_policies") == 1


def test_errors_the_change_did_not_cause_do_not_block_a_put(spoke_dir: Path) -> None:
    """if a pre-existing fleet error (no hub, unseeded kinds) blocks an unrelated PUT then broken"""
    me = local_id(spoke_dir)
    with http_client(spoke_dir) as http:
        fleet = http.post("/api/v1/cloudsync/policies/validate").json()
        assert http.post("/api/v1/cloudsync/policies/validate", json={}).json() == fleet
        put = http.put(
            "/api/v1/cloudsync/policies",
            json={"machine_id": me, "asset_kind": "audio", "mode": "pinned"},
        )
    stored = {v["rule_id"] for v in fleet["violations"] if v["severity"] == "error"}
    assert {"single_hub", "completeness"} <= stored, "the control: the fleet IS already broken"
    assert not any(v["introduced"] for v in fleet["violations"])
    assert put.status_code == 200, put.text


def _store_pre_gate_cell(data_dir: Path, machine_id: str, mode: str, budget: int | None) -> None:
    """An ``audio`` cell as a pre-gate client or an older peer's sync left it: the
    gate never judged it, so it can already break a rule."""
    conn = sqlite3.connect(sync_client.state_db_path(data_dir))
    try:
        conn.execute(
            "INSERT INTO sync_policies(machine_id, asset_kind, mode, cache_budget_mb, "
            "updated_at, origin_device_id, deleted_at) VALUES (?, 'audio', ?, ?, ?, ?, NULL)",
            (machine_id, mode, budget, STAMP, machine_id),
        )
        conn.commit()
    finally:
        conn.close()


def _stored_audio_cell(data_dir: Path, machine_id: str) -> tuple[Any, ...]:
    conn = sqlite3.connect(sync_client.state_db_path(data_dir))
    try:
        return tuple(
            conn.execute(
                "SELECT mode, cache_budget_mb FROM sync_policies "
                "WHERE machine_id = ? AND asset_kind = 'audio'",
                (machine_id,),
            ).fetchone()
        )
    finally:
        conn.close()


def test_rewriting_a_broken_cell_with_another_invalid_value_is_409(spoke_dir: Path) -> None:
    """if a PUT or apply may rewrite a broken cell with another invalid value then broken"""
    me = local_id(spoke_dir)
    _store_pre_gate_cell(spoke_dir, me, "stream", 512)
    # 'pinned' + a budget breaks only cache_budget ('excluded' would also add durability).
    still_invalid: dict[str, Any] = {
        "machine_id": me,
        "asset_kind": "audio",
        "mode": "pinned",
        "cache_budget_mb": 99999,
    }
    logged = count(spoke_dir, "SELECT COUNT(*) FROM local_changelog")
    with http_client(spoke_dir) as http:
        put = http.put("/api/v1/cloudsync/policies", json=still_invalid)
        applied = http.post(
            "/api/v1/cloudsync/policies/apply",
            json={"changes": {"policies": [still_invalid]}, "dry_run": False},
        )
    assert put.status_code == applied.status_code == 409, put.text
    blocking = [
        (v["rule_id"], v["subject"], v["introduced"])
        for v in put.json()["detail"]["outcome"]["violations"]
        if v["blocking"]
    ]
    # Not introduced (the cell was already broken), yet blocking: it is the PUT's own target.
    assert blocking == [("cache_budget", f"{me}/audio", False)]
    assert _stored_audio_cell(spoke_dir, me) == ("stream", 512)
    assert count(spoke_dir, "SELECT COUNT(*) FROM local_changelog") == logged


def test_validate_sweep_surfaces_pre_existing_negative_cache_budget(spoke_dir: Path) -> None:
    """if a pre-gate negative cache_budget row is not named by validate then broken"""
    me = local_id(spoke_dir)
    _store_pre_gate_cell(spoke_dir, me, "cached", -42)
    with http_client(spoke_dir) as http:
        outcome = http.post("/api/v1/cloudsync/policies/validate", json={}).json()
    cache = [v for v in outcome["violations"] if v["rule_id"] == "cache_budget"]
    assert cache, outcome["violations"]
    assert cache[0]["subject"] == f"{me}/audio"
    assert not cache[0]["blocking"]


def test_rewriting_a_broken_cell_to_a_valid_value_is_accepted(spoke_dir: Path) -> None:
    """if a PUT that repairs an already broken cell is refused then broken"""
    me = local_id(spoke_dir)
    _store_pre_gate_cell(spoke_dir, me, "stream", 512)
    with http_client(spoke_dir) as http:
        put = http.put(
            "/api/v1/cloudsync/policies",
            json={
                "machine_id": me,
                "asset_kind": "audio",
                "mode": "cached",
                "cache_budget_mb": 512,
            },
        )
    assert put.status_code == 200, put.text
    assert _stored_audio_cell(spoke_dir, me) == ("cached", 512)


# ----- overview is machine-scoped ------------------------------------------------------


def test_another_machines_copy_does_not_hydrate_a_pinned_machine(spoke_dir: Path) -> None:
    """if machine A's local copy counts as hydration for machine B's pins then broken"""
    me = local_id(spoke_dir)
    conn = sqlite3.connect(sync_client.state_db_path(spoke_dir))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        add_remote_machine(conn, OTHER_ID, is_hub=0)
        for stable_id in TRACKS:
            add_local_copy(conn, stable_id, me)
        conn.commit()
    finally:
        conn.close()
    with http_client(spoke_dir) as http:
        for machine_id in (me, OTHER_ID):
            assert (
                http.put(
                    "/api/v1/cloudsync/playlist-pins",
                    json={"machine_id": machine_id, "playlist_id": PLAYLIST_ID, "mode": "pinned"},
                ).status_code
                == 200
            )
        overview = http.get("/api/v1/cloudsync/overview").json()
    unhydrated = {m["machine_id"]: m["unhydrated_pinned_count"] for m in overview["machines"]}
    assert unhydrated == {me: 0, OTHER_ID: len(TRACKS)}
