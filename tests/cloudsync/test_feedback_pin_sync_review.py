"""PR #1978 review fixes for feedback pin sync (FBSYNC-01..04, ADR-0013).

Same harness as ``test_feedback_pin_sync.py``: two real engines and one real
CloudSync hub behind uvicorn on the loopback, every sync real HTTP through the
production transport. The ``hub_url``/``air``/``silver`` fixtures are
duplicated rather than imported, the convention ``test_hub_sync_protocol.py``
documents (a fixture re-exported by import collides, pyflakes F811, with the
same-named parameter); the substantial helpers are imported so they cannot
drift.

Regression lines (each is one test below, and each was made to fail by
reverting its fix; see the PR body for the output):
  - if a bulk harvest on one machine empties another machine's board, a local
    harvest is being replicated as a tombstone -- broken (FBSYNC-03)
  - if a pin field this build does not know is dropped on a round trip, or the
    pin keeps re-importing, mixed builds lose data or churn -- broken (FBSYNC-02)
  - if a failure between writing the temp file and the rename changes the
    store, pin writes can tear -- broken (FBSYNC-01)
  - if an unexpected error kills the scheduler thread or goes unreported,
    scheduled sync dies silently -- broken (FBSYNC-04)
  - if a misconfigured scheduler stops the engine booting, sync can take
    playback down -- broken (FBSYNC-04)
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.sync_hub import client as sync_client
from apps.sync_hub import service as sync_service
from apps.sync_hub import status as sync_status
from apps.webui.server.app import create_app
from apps.webui.server.cloudsync_scheduler import CloudSyncScheduler
from tests.cloudsync.test_feedback_pin_sync import _T_NEW, Engine, _engine, _pin_doc, _serve
from tests.waits import wait_for_external_state

_UNKNOWN = "zz_field_from_a_newer_build"


@pytest.fixture
def hub_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """The real sync router behind uvicorn; both engines point at it."""
    monkeypatch.delenv("MDT_IS_HUB", raising=False)
    monkeypatch.delenv(sync_status.SCHEDULER_ENV, raising=False)
    hub_dir = tmp_path / "hub"
    hub_dir.mkdir()
    state_db.open_rw(sync_client.state_db_path(hub_dir)).close()
    app = FastAPI()
    app.state.state_db_path = str(sync_client.state_db_path(hub_dir))
    app.state.sync_hub_data_dir = str(hub_dir)
    app.state.sync_hub_machine_name = "hub"
    app.include_router(sync_service.router, prefix="/api/v1")
    for url in _serve(app, "the feedback pin hub"):
        monkeypatch.setenv(sync_status.ENDPOINT_ENV, url)
        yield url


@pytest.fixture
def air(tmp_path: Path, hub_url: str) -> Iterator[Engine]:
    yield from _engine(tmp_path, "air")


@pytest.fixture
def silver(tmp_path: Path, hub_url: str) -> Iterator[Engine]:
    yield from _engine(tmp_path, "silver")


def _stored_comments(engine: Engine) -> dict[str, dict[str, Any]]:
    """comments.json as written to disk, not as the list route renders it."""
    raw = json.loads((engine.feedback_dir / "comments.json").read_text(encoding="utf-8"))
    return {comment["id"]: comment for comment in raw["comments"]}


def _stored_row_doc(engine: Engine, pin_id: str) -> dict[str, Any]:
    row = engine.row(pin_id)
    assert row is not None, f"{pin_id} never reached {engine.name}'s feedback_pins"
    doc: dict[str, Any] = json.loads(row[0])
    return doc


# ----- P1-1: a bulk harvest is local, never a synced tombstone ---------------


@pytest.mark.requirement("FBSYNC-03")
def test_a_bulk_harvest_on_one_machine_never_empties_another_machines_board(
    air: Engine, silver: Engine
) -> None:
    """[if] silver bulk-harvests air's open pin [then] air keeps it on its board, [else stop]."""
    pin = air.drop("open on air, harvested on silver")
    air.sync()
    silver.sync()

    harvested = silver.http.post("/api/v1/feedback/archive")
    assert harvested.status_code == 200, harvested.text
    assert harvested.json()["comments_archived"] == 1
    silver.sync()
    air.sync()

    assert pin["id"] in air.pins(), "silver's bulk harvest emptied air's board"
    row = air.row(pin["id"])
    assert row is not None and row[3] is None, "a bulk harvest became a synced tombstone"

    # The harvest still holds on the machine that ran it, and does not churn.
    again = silver.sync()
    assert (again["exported"], again["imported"]) == (0, 0), again
    assert pin["id"] in silver.pins(), "silver harvest removed pin from live board"
    assert silver.pins()[pin["id"]]["status"] == "harvested"
    assert silver.status(pin["id"])["pins"][0]["state"] == "harvested"

    # A later edit on another machine brings it back, like any newer edit.
    air.patch(pin["id"], agent_note="still broken after the harvest")
    air.sync()
    silver.sync()
    assert silver.pins()[pin["id"]]["agent_note"] == "still broken after the harvest"


@pytest.mark.requirement("FB-15")
def test_a_sync_tombstone_loses_to_a_newer_local_reply(
    air: Engine, silver: Engine
) -> None:
    """[if] local reply is newer than tombstone [then] tombstone is rejected, [else stop]."""
    pin = air.drop("survives tombstone")
    air.patch(pin["id"], status="fixed")
    air.sync()
    silver.sync()

    archived = silver.http.post(f"/api/v1/feedback/comments/{pin['id']}/archive")
    assert archived.status_code == 200, archived.text
    silver.sync()

    air.http.post(
        f"/api/v1/feedback/comments/{pin['id']}/replies",
        json={"text": "still open after fix attempt"},
    )
    air.sync()

    assert pin["id"] in air.pins(), "newer local reply lost to sync tombstone"
    assert air.pins()[pin["id"]]["status"] == "open"
    row = air.row(pin["id"])
    assert row is not None and row[3] is None, "tombstone won over newer local reply"


# ----- P1-2: version skew between builds -------------------------------------


@pytest.mark.requirement("FBSYNC-02")
def test_a_field_this_build_does_not_know_survives_the_round_trip_and_stops_churning(
    air: Engine, silver: Engine
) -> None:
    """[if] a pin carries a field no engine knows [then] it survives and settles, [else stop]."""
    # One pin seeded on each side, so both machine-id orders are exercised
    # whichever id each engine happens to draw. Equal edit instants make the
    # tie rule, not the clock, decide what an old build does with the copy.
    from_air = {**_pin_doc("aaaa00000001", "from air", _T_NEW, "air"), _UNKNOWN: {"v": 1}}
    from_air["build"] = {**from_air["build"], "zz_nested": "kept"}
    from_silver = {**_pin_doc("bbbb00000002", "from silver", _T_NEW, "silver"), _UNKNOWN: 2}
    air.seed(from_air)
    silver.seed(from_silver)

    air.sync()
    silver.sync()
    air.sync()
    silver.sync()

    for engine in (air, silver):
        stored = _stored_comments(engine)
        assert stored[from_air["id"]][_UNKNOWN] == {"v": 1}, engine.name
        assert stored[from_air["id"]]["build"]["zz_nested"] == "kept", engine.name
        assert stored[from_silver["id"]][_UNKNOWN] == 2, engine.name
        assert _stored_row_doc(engine, from_air["id"])[_UNKNOWN] == {"v": 1}, engine.name
        assert _stored_row_doc(engine, from_silver["id"])[_UNKNOWN] == 2, engine.name

    for engine in (air, silver, air, silver):
        settled = engine.sync()
        assert (settled["exported"], settled["imported"]) == (0, 0), (engine.name, settled)
        states = {pin["state"] for pin in engine.status()["pins"]}
        assert states == {"synced"}, (engine.name, states)


@pytest.mark.requirement("FBSYNC-02")
def test_a_late_store_copy_at_the_same_instant_defers_to_the_synced_row(
    air: Engine, silver: Engine
) -> None:
    """[if] a late store copy ties a synced row [then] the synced row stays, [else stop]."""
    # The machine with the GREATER id gets the late copy: that is the order in
    # which breaking the tie on the local id would re-export it under its own
    # name and overwrite the synced copy on every machine.
    hi, lo = (air, silver) if air.machine_id() > silver.machine_id() else (silver, air)
    shared = "dddd00000004"
    lo.seed(_pin_doc(shared, "the synced copy", _T_NEW, lo.name))
    lo.sync()
    hi.sync()
    assert hi.pins()[shared]["text"] == "the synced copy"

    # The one-off merge of the old per-machine stores drops a different copy
    # of the same pin, stamped at the same instant, into hi's store.
    hi.seed(_pin_doc(shared, "a late store copy", _T_NEW, hi.name))
    hi.sync()
    lo.sync()
    hi.sync()

    for engine in (hi, lo):
        assert engine.pins()[shared]["text"] == "the synced copy", engine.name
        row = engine.row(shared)
        assert row is not None and row[2] == lo.machine_id(), (
            f"{engine.name}: the tie was re-exported under the late machine's id"
        )


# ----- P2-1: torn writes -----------------------------------------------------


@pytest.mark.requirement("FBSYNC-01")
def test_a_crash_between_temp_write_and_rename_leaves_the_old_store_intact(
    air: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a write dies before its rename [then] the old file is intact, [else stop]."""
    pin = air.drop("survives a torn write")
    air.patch(pin["id"], status="fixed")
    before = (air.feedback_dir / "comments.json").read_bytes()

    def crash_before_rename(src: Any, dst: Any) -> None:
        raise OSError(f"simulated crash before renaming {src} over {dst}")

    monkeypatch.setattr(os, "replace", crash_before_rename)
    with pytest.raises(OSError, match="simulated crash"):
        air.patch(pin["id"], agent_note="never lands")
    with pytest.raises(OSError, match="simulated crash"):
        air.http.post(f"/api/v1/feedback/comments/{pin['id']}/archive")
    monkeypatch.undo()

    assert (air.feedback_dir / "comments.json").read_bytes() == before
    assert not list(air.feedback_dir.glob("archive-*.json")), "a torn archive file was left"
    assert sorted(p.name for p in air.feedback_dir.iterdir()) == ["comments.json"], (
        "a temp file was left behind"
    )
    assert air.pins()[pin["id"]]["status"] == "fixed"


# ----- P2-2 / P2-3: the scheduler survives and never blocks boot -------------


@pytest.mark.requirement("FBSYNC-04")
def test_the_scheduler_survives_an_unexpected_error_and_reports_it(
    air: Engine, hub_url: str
) -> None:
    """[if] a tick raises something unexpected [then] it keeps ticking and says so, [else stop]."""
    scheduler = CloudSyncScheduler(air.app, interval_s=0.05, env={
        sync_status.SCHEDULER_ENV: "1", sync_status.ENDPOINT_ENV: hub_url,
    })
    air.app.state.cloudsync_scheduler = scheduler
    air.feedback_dir.mkdir(parents=True, exist_ok=True)
    (air.feedback_dir / "comments.json").write_text("{torn", encoding="utf-8")
    scheduler.start()
    try:
        wait_for_external_state(
            lambda: scheduler.status().last_error is not None, what="the first failed tick"
        )
        failed_ticks = scheduler.status().ticks
        wait_for_external_state(
            lambda: scheduler.status().ticks >= failed_ticks + 3, what="three more ticks"
        )
        assert scheduler.running, "an unexpected error killed the scheduler thread"
        journal = air.http.get("/api/v1/cloudsync/status").json()
        assert journal["last_result"]["status"] == "error"
        assert "JSONDecodeError" in journal["last_result"]["message"]

        air.seed(_pin_doc("cccc00000003", "after the repair", _T_NEW, "air"))
        wait_for_external_state(
            lambda: scheduler.status().last_ok_at is not None, what="a good tick after repair"
        )
        reported = air.status()["scheduler"]
        assert reported["state"] == "running" and reported["alive"] is True
        assert "JSONDecodeError" in reported["last_error"]
        assert reported["last_error_at"] is not None and reported["last_ok_at"] is not None
    finally:
        scheduler.stop()
    assert air.status()["scheduler"]["state"] == "stopped"


@pytest.mark.requirement("FBSYNC-04")
def test_a_misconfigured_scheduler_never_blocks_engine_boot(
    tmp_path: Path, hub_url: str, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """[if] the scheduler cannot resolve its stores [then] the engine still boots, [else stop]."""
    monkeypatch.setenv(sync_status.SCHEDULER_ENV, "1")
    data_dir = tmp_path / "split"
    state_db.open_rw(sync_client.state_db_path(data_dir)).close()
    app = create_app(
        state_db_path=str(sync_client.state_db_path(data_dir)), hostname="split",
        version="9.9.9-test", mount_frontend=False, enable_cors=False,
        cloudsync_scheduler=True,
    )
    app.state.data_dir = tmp_path / "somewhere-else"  # the split layout sync refuses

    with caplog.at_level(logging.ERROR), TestClient(app) as http:
        assert http.get("/api/v1/health").status_code == 200, "the engine did not boot"
        scheduler = app.state.cloudsync_scheduler
        assert not scheduler.running
        assert scheduler.status().state == "misconfigured"
        assert "FEEDBACK_SYNC_LAYOUT" in (scheduler.status().reason or "")
        journal = http.get("/api/v1/cloudsync/status").json()
        assert journal["last_result"]["status"] == "error"
        assert "FEEDBACK_SYNC_LAYOUT" in journal["last_result"]["message"]
    assert any(
        record.levelno == logging.ERROR and "FEEDBACK_SYNC_LAYOUT" in record.getMessage()
        for record in caplog.records
    ), "the misconfiguration was not logged at ERROR"


@pytest.mark.requirement("FBSYNC-04")
def test_a_scheduler_switch_that_is_not_0_or_1_is_a_loud_misconfiguration(
    air: Engine, hub_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """[if] the switch reads 'yes' [then] it is a WARNING and misconfigured, [else stop]."""
    scheduler = CloudSyncScheduler(air.app, env={
        sync_status.SCHEDULER_ENV: "yes", sync_status.ENDPOINT_ENV: hub_url,
    })
    with caplog.at_level(logging.WARNING):
        scheduler.start()
    assert not scheduler.running
    assert scheduler.status().state == "misconfigured"
    assert any(
        record.levelno >= logging.WARNING and "'yes'" in record.getMessage()
        for record in caplog.records
    )
