"""Sync health on the CloudSync page: the readout that ends the invisibility.

Round 4 left the spoke protocol correct and INVISIBLE -- ``run_sync``
returned a rich result to whichever pytest called it and nothing kept it, so
a DJ with two laptops could not see whether they agreed. These tests hold the
``sync`` block on ``GET /cloudsync/overview`` and hold it IDENTICAL to
``FleetHealth.to_wire()``.

Split from ``test_cloudsync_routes.py`` (quality-gate ``file_size`` ratchet);
its fixtures are imported rather than duplicated, so both files describe one
app wired one way.

Regression one-liners:
  - if GET /cloudsync/overview drops the sync block then broken
  - if the sync block's keys drift from FleetHealth.to_wire then broken
  - if a never-synced machine reports a verdict other than never_synced then broken
  - if an unpushed UI edit is not counted as pending push rows then broken
  - if a completed sync does not flip the verdict to converged then broken
  - if a recorded digest mismatch does not reach the page then broken
  - if an unreadable status file renders as an empty readout then broken
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
from apps.sync_hub import health as sync_health
from apps.sync_hub import service as sync_service
from apps.webui.server.routes import cloudsync_overview

# The app wiring, the seeded DB and the hub transport are imported rather
# than duplicated, so both files describe the SAME app wired the same way.
# The fixtures themselves are re-declared below (a fixture imported by name
# and then taken as a parameter reads to ruff as a redefinition, and the
# ruff config belongs to another lane).
from tests.webui.test_cloudsync_routes import (
    _HubTransport,
    _make_client,
    _registered_machine_id,
    _seed_state_db,
    _spoke_data_dir,
)


@pytest.fixture
def state_db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state" / "state.db"
    _seed_state_db(path)
    return path


@pytest.fixture
def client(state_db_path: Path) -> Iterator[TestClient]:
    with _make_client(state_db_path) as http:
        http.base_url = "http://127.0.0.1"
        yield http


@pytest.fixture
def hub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_HubTransport]:
    monkeypatch.delenv("MDT_IS_HUB", raising=False)
    hub_dir = tmp_path / "hub"
    app = FastAPI()
    app.state.state_db_path = str(sync_client.state_db_path(hub_dir))
    app.state.sync_hub_data_dir = str(hub_dir)
    app.state.sync_hub_machine_name = "cloudsync-health-hub"
    app.include_router(sync_service.router, prefix="/api/v1")
    with TestClient(app) as http:
        yield _HubTransport(http)


def _overview(client: TestClient) -> dict[str, Any]:
    r = client.get("/api/v1/cloudsync/overview")
    assert r.status_code == 200, r.text
    return r.json()


def test_overview_carries_a_sync_block_on_a_fresh_machine(client: TestClient):
    body = _overview(client)
    assert body["sync"] is not None, "a page with no sync readout cannot answer the question the feature exists for"
    block = body["sync"]
    assert block["verdict"] == "never_synced"
    assert block["peers"] == []
    assert block["last_run"] is None
    assert block["pending_push_rows"] is None, "0 would read as converged on a machine that has never met a hub"
    assert block["digest_mismatch"] is False


def test_the_sync_block_is_exactly_the_cli_readout(
    client: TestClient,
    state_db_path: Path,
):
    """Agent parity: one computation, two renderers.

    ``GET /cloudsync/overview``'s ``sync`` block is
    ``FleetHealth.to_wire()``. A field added to one and not the other is
    the drift this pins. CSSTATUS (``maintenance.status``) is a different
    readout and is not compared here.
    """
    data_dir = _spoke_data_dir(state_db_path)
    conn = sqlite3.connect(str(state_db_path))
    try:
        wire = sync_health.fleet_health(conn, data_dir).to_wire()
    finally:
        conn.close()

    block = _overview(client)["sync"]
    assert block == wire, "the page must answer with FleetHealth.to_wire()"
    assert set(cloudsync_overview.SyncHealthOut.model_fields) == set(wire), (
        "a field added to FleetHealth.to_wire and not to the response model is dropped on the way to the page"
    )
    assert block["verdict"] in sync_health.VERDICTS


def test_an_unpushed_ui_edit_shows_up_as_pending_push_rows(
    client: TestClient,
    state_db_path: Path,
    hub: _HubTransport,
):
    machine_id = _registered_machine_id(client)
    data_dir = _spoke_data_dir(state_db_path)

    sync_client.run_sync(data_dir, "http://hub.invalid", transport=hub)
    sync_health.record_outcome(
        data_dir,
        sync_health.SyncRun(
            started_at="2026-09-01T09:00:00.000000+00:00",
            finished_at="2026-09-01T09:00:03.000000+00:00",
            outcome=sync_health.OK_OUTCOME,
            hub_url="http://hub.invalid",
            consecutive_failures=0,
        ),
    )
    settled = _overview(client)["sync"]
    assert settled["verdict"] == "converged"
    assert settled["pending_push_rows"] == 0
    assert len(settled["peers"]) == 1

    client.put(
        "/api/v1/cloudsync/policies",
        json={
            "machine_id": machine_id,
            "asset_kind": "audio",
            "mode": "pinned",
        },
    )

    behind = _overview(client)["sync"]
    assert behind["pending_push_rows"] == 1, (
        "an edit the hub has not seen is exactly what a DJ needs to see before walking out with the laptop"
    )
    assert behind["verdict"] == "behind"
    assert behind["peers"][0]["pending_push_rows"] == 1

    sync_client.run_sync(data_dir, "http://hub.invalid", transport=hub)
    assert _overview(client)["sync"]["pending_push_rows"] == 0


def test_a_recorded_failure_reaches_the_overview(
    client: TestClient,
    state_db_path: Path,
):
    sync_health.record_outcome(
        _spoke_data_dir(state_db_path),
        sync_health.SyncRun(
            started_at="2026-09-01T09:00:00.000000+00:00",
            finished_at="2026-09-01T09:00:02.000000+00:00",
            outcome="digest_mismatch",
            hub_url="http://hub.invalid",
            consecutive_failures=0,
            error_kind="SyncDigestMismatch",
            error_message="tables ['tracks'] differ",
        ),
    )
    block = _overview(client)["sync"]
    assert block["verdict"] == "diverged"
    assert block["digest_mismatch"] is True
    assert block["last_run"]["error_kind"] == "SyncDigestMismatch"
    assert block["last_run"]["consecutive_failures"] == 1


def test_an_unreadable_status_file_is_a_500_not_an_empty_readout(
    client: TestClient,
    state_db_path: Path,
):
    sync_health.status_path(_spoke_data_dir(state_db_path)).write_text("{ truncated", encoding="utf-8")
    r = client.get("/api/v1/cloudsync/overview")
    assert r.status_code == 500
    assert r.json()["detail"]["code"] == "CLOUDSYNC_HEALTH_UNREADABLE"


pytestmark = pytest.mark.rb_parity
