"""FBSYNC-01..05: feedback comment pins sync across machines through CloudSync.

Two real engines (the webui app every Open DJ engine serves, each on its own
data dir, as the Air and Silver desktop apps are) and one real CloudSync hub
behind uvicorn on the loopback. Every sync below is real HTTP through the
production ``HttpTransport``, driven by the real ``POST /feedback/sync``; every
pin is dropped, patched, archived and given a screenshot through the real
feedback routes. Where a test seeds ``comments.json`` directly it is standing
up the pre-existing per-machine stores this feature has to merge, which is the
state the real stores were in on Fri 11 Sep 2026.

Regression lines (each is one test below, and each was made to fail by
injecting the named defect; see the PR body for the output):
  - if a pin dropped on A is not on B, field for field, after A syncs then B
    syncs, pins are not replicating -- broken (FBSYNC-01)
  - if a status change, agent note or follow-on made on B does not reach A,
    lifecycle is not replicating -- broken (FBSYNC-02)
  - if two machines edit one pin before either syncs and they do not converge
    on the later edit, the conflict rule is not last-writer-wins -- broken
    (FBSYNC-02)
  - if an exact-instant tie is not broken by the greater machine id on both
    machines, the tiebreak is not deterministic -- broken (FBSYNC-02)
  - if an archive on B does not tombstone the pin on A, or a later sync
    resurrects it, archive is not a synced tombstone -- broken (FBSYNC-03)
  - if a pin missing from one machine's comments.json is treated as deleted,
    absence is being read as deletion -- broken (FBSYNC-03)
  - if a hub outage is anything but a loud 503 plus an error journal, or the
    pins made meanwhile do not arrive once it is back, offline-first is
    broken (FBSYNC-04)
  - if one pin id held by two stores does not collapse to one pin, the
    pre-merge stores duplicate forever -- broken (FBSYNC-01)
  - if a synced attachment's missing bytes are not reported as such in the
    API, the gap is silent -- broken (FBSYNC-05)
"""
from __future__ import annotations

import io
import json
import socket
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from apps.shared.state import db as state_db
from apps.shared.state import sync_stamp
from apps.sync_hub import client as sync_client
from apps.sync_hub import maintenance
from apps.sync_hub import service as sync_service
from apps.sync_hub import status as sync_status
from apps.webui.server.app import create_app
from apps.webui.server.cloudsync_scheduler import CloudSyncScheduler
from tests.waits import start_uvicorn_in_thread

_T_OLD = "2026-09-01T09:00:00.000000Z"
_T_NEW = "2026-09-02T09:00:00.000000Z"


# ----- a real hub, real engines --------------------------------------------


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _serve(app: FastAPI, what: str) -> Iterator[str]:
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server, thread = start_uvicorn_in_thread(config, what=what)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)


@pytest.fixture
def hub_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """The real sync router behind uvicorn, on an empty hub DB.

    Also points both engines at it through ``MDT_CLOUDSYNC_HUB_URL``, the one
    variable the engines read their hub from, and clears the scheduler switch
    so no background tick races a test's explicit syncs.
    """
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


@dataclass
class Engine:
    """One machine's engine: its data dir and the real app over ASGI."""

    name: str
    data_dir: Path
    http: TestClient

    @property
    def feedback_dir(self) -> Path:
        return self.data_dir / "feedback"

    def pins(self) -> dict[str, dict[str, Any]]:
        response = self.http.get("/api/v1/feedback/comments")
        assert response.status_code == 200, response.text
        comments = response.json()["comments"]
        ids = [comment["id"] for comment in comments]
        assert len(ids) == len(set(ids)), f"{self.name} lists a pin twice: {ids}"
        return {comment["id"]: comment for comment in comments}

    def drop(self, text: str) -> dict[str, Any]:
        response = self.http.post("/api/v1/feedback/comments", json={
            "x_pct": 12.5, "y_pct": 40.0, "anchor": "deck-a-waveform",
            "page": "/performance", "text": text, "ui": "packaged-app",
            "viewport_width": 1512, "viewport_height": 982,
        })
        assert response.status_code == 201, response.text
        return response.json()

    def patch(self, pin_id: str, **changes: Any) -> dict[str, Any]:
        response = self.http.patch(f"/api/v1/feedback/comments/{pin_id}", json=changes)
        assert response.status_code == 200, response.text
        return response.json()

    def sync(self) -> dict[str, Any]:
        response = self.http.post("/api/v1/feedback/sync")
        assert response.status_code == 200, f"{self.name} sync: {response.text}"
        return response.json()

    def status(self, pin_id: str | None = None) -> dict[str, Any]:
        params = {} if pin_id is None else {"pin_id": pin_id}
        response = self.http.get("/api/v1/feedback/sync/status", params=params)
        assert response.status_code == 200, response.text
        return response.json()

    def machine_id(self) -> str:
        conn = state_db.open_rw(sync_client.state_db_path(self.data_dir))
        try:
            return sync_stamp.ensure_local_machine(conn)
        finally:
            conn.close()

    def row(self, pin_id: str) -> tuple[Any, ...] | None:
        conn = sqlite3.connect(sync_client.state_db_path(self.data_dir))
        try:
            return conn.execute(
                "SELECT doc, updated_at, origin_device_id, deleted_at "
                "FROM feedback_pins WHERE pin_id = ?", (pin_id,),
            ).fetchone()
        finally:
            conn.close()

    def seed(self, *docs: dict[str, Any]) -> None:
        """Stand up a pre-existing store, as the four real ones were."""
        self.feedback_dir.mkdir(parents=True, exist_ok=True)
        (self.feedback_dir / "comments.json").write_text(
            json.dumps({"comments": list(docs)}, indent=2), encoding="utf-8"
        )


def _engine(tmp_path: Path, name: str) -> Iterator[Engine]:
    data_dir = tmp_path / name
    state_db.open_rw(sync_client.state_db_path(data_dir)).close()
    app = create_app(
        state_db_path=str(sync_client.state_db_path(data_dir)),
        hostname=name,
        version="9.9.9-test",
        mount_frontend=False,
        enable_cors=False,
    )
    app.state.data_dir = data_dir
    with TestClient(app) as http:
        yield Engine(name=name, data_dir=data_dir, http=http)


@pytest.fixture
def air(tmp_path: Path, hub_url: str) -> Iterator[Engine]:
    yield from _engine(tmp_path, "air")


@pytest.fixture
def silver(tmp_path: Path, hub_url: str) -> Iterator[Engine]:
    yield from _engine(tmp_path, "silver")


def _pin_doc(pin_id: str, text: str, updated_at: str, machine: str) -> dict[str, Any]:
    return {
        "id": pin_id, "x_pct": 50.0, "y_pct": 50.0, "anchor": None,
        "page": "/performance", "text": text, "created_at": _T_OLD,
        "updated_at": updated_at, "status": "open",
        "build": {"git_sha": "abcdef12", "built_at_utc": _T_OLD, "source": "repo"},
        "environment": {
            "ui": "packaged-app", "viewport_width": 1512, "viewport_height": 982,
            "machine": machine, "release_version": "1.0.0",
        },
    }


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), (255, 0, 0)).save(buffer, format="PNG")
    return buffer.getvalue()


# ----- FBSYNC-01: pins replicate intact -------------------------------------


@pytest.mark.requirement("FBSYNC-01")
def test_a_pin_dropped_on_one_machine_arrives_on_the_other_field_for_field(
    air: Engine, silver: Engine
) -> None:
    """[if] a pin dropped on air syncs to silver [then] every field matches, [else stop]."""
    dropped = air.drop("loop band missing on deck B")
    air.sync()
    assert dropped["id"] not in silver.pins()
    silver.sync()

    arrived = silver.pins()[dropped["id"]]
    assert arrived == air.pins()[dropped["id"]], "a field changed in transit"
    assert arrived["environment"]["machine"] == "air", "provenance lost in transit"
    assert (arrived["page"], arrived["anchor"], arrived["x_pct"], arrived["y_pct"]) == (
        "/performance", "deck-a-waveform", 12.5, 40.0,
    )
    assert silver.status(dropped["id"])["pins"][0]["state"] == "synced"


@pytest.mark.requirement("FBSYNC-01")
def test_one_pin_id_held_by_two_stores_collapses_to_one_pin(
    air: Engine, silver: Engine
) -> None:
    """[if] two stores share a pin id [then] both end with one pin, the newer, [else stop]."""
    shared = "0123456789ab"
    air.seed(
        _pin_doc(shared, "old copy", _T_OLD, "air"),
        _pin_doc("aaaaaaaaaaaa", "only on air", _T_OLD, "air"),
    )
    silver.seed(_pin_doc(shared, "newer copy", _T_NEW, "air"))

    # The machine holding the NEWER copy syncs first, so the stale store is
    # synced late. That is the order that catches a stamp taken at sync time
    # rather than edit time: under that defect the late, stale copy would win.
    silver.sync()
    air.sync()
    silver.sync()

    for engine in (air, silver):
        pins = engine.pins()
        assert set(pins) == {shared, "aaaaaaaaaaaa"}, engine.name
        assert pins[shared]["text"] == "newer copy", engine.name


# ----- FBSYNC-02: lifecycle and conflicts ------------------------------------


@pytest.mark.requirement("FBSYNC-02")
def test_status_note_and_follow_on_made_on_one_machine_reach_the_other(
    air: Engine, silver: Engine
) -> None:
    """[if] silver fixes, notes and follows on [then] air shows all three, [else stop]."""
    pin = air.drop("filter sweep clicks")
    air.sync()
    silver.sync()

    silver.patch(pin["id"], status="fixed", agent_note="fixed in #2001",
                 issue_url="https://github.com/o/r/issues/2001")
    follow_on = silver.http.post(
        f"/api/v1/feedback/comments/{pin['id']}/follow-on", json={"text": "still at 2x"}
    )
    assert follow_on.status_code == 201, follow_on.text
    silver.sync()
    air.sync()

    on_air = air.pins()
    assert on_air[pin["id"]]["status"] == "fixed"
    assert on_air[pin["id"]]["agent_note"] == "fixed in #2001"
    assert on_air[follow_on.json()["id"]]["text"].startswith("Follow-on to https://")


@pytest.mark.requirement("FBSYNC-02")
def test_concurrent_edits_converge_on_the_later_edit_everywhere(
    air: Engine, silver: Engine
) -> None:
    """[if] both edit before syncing [then] both keep the later edit, [else stop]."""
    pin = air.drop("crossfader curve")
    air.sync()
    silver.sync()

    air.patch(pin["id"], agent_note="air's edit")
    later = silver.patch(pin["id"], agent_note="silver's later edit")
    air.sync()
    silver.sync()
    air.sync()

    for engine in (air, silver):
        assert engine.pins()[pin["id"]]["agent_note"] == "silver's later edit", engine.name
        assert engine.pins()[pin["id"]]["updated_at"] == later["updated_at"], engine.name


@pytest.mark.requirement("FBSYNC-02")
def test_an_exact_instant_tie_is_won_by_the_greater_machine_id_everywhere(
    air: Engine, silver: Engine
) -> None:
    """[if] both copies share one instant [then] the greater machine id wins, [else stop]."""
    tied = "bbbbbbbbbbbb"
    air.seed(_pin_doc(tied, "air says", _T_NEW, "air"))
    silver.seed(_pin_doc(tied, "silver says", _T_NEW, "silver"))
    winner = "air says" if air.machine_id() > silver.machine_id() else "silver says"

    air.sync()
    silver.sync()
    air.sync()

    for engine in (air, silver):
        assert engine.pins()[tied]["text"] == winner, engine.name


# ----- FBSYNC-03: archive is a tombstone, absence is not ---------------------


@pytest.mark.requirement("FBSYNC-03")
def test_an_archive_on_one_machine_tombstones_the_pin_everywhere(
    air: Engine, silver: Engine
) -> None:
    """[if] silver archives a pin [then] air tombstones it for good, [else stop]."""
    pin = air.drop("stem mute lag")
    air.sync()
    silver.sync()

    silver.patch(pin["id"], status="merged")
    archived = silver.http.post(f"/api/v1/feedback/comments/{pin['id']}/archive")
    assert archived.status_code == 200, archived.text
    silver.sync()
    air.sync()

    assert pin["id"] not in air.pins()
    row = air.row(pin["id"])
    assert row is not None and row[3] is not None, "archive did not arrive as a tombstone"
    archived_on_air = [
        comment
        for path in sorted(air.feedback_dir.glob("archive-*.json"))
        for comment in json.loads(path.read_text())["comments"]
        if comment["id"] == pin["id"]
    ]
    assert archived_on_air and archived_on_air[-1]["status"] == "archived"
    assert air.status(pin["id"])["pins"][0]["archived"] is True

    air.sync()
    silver.sync()
    assert pin["id"] not in air.pins() and pin["id"] not in silver.pins(), "resurrected"


@pytest.mark.requirement("FBSYNC-03")
def test_a_pin_missing_from_one_store_is_restored_not_deleted(
    air: Engine, silver: Engine
) -> None:
    """[if] a pin vanishes from a store [then] sync restores it, [else stop]."""
    pin = air.drop("jog wheel drift")
    air.sync()
    silver.sync()

    silver.seed()  # the store is rewritten without the pin, by hand or by a merge
    silver.sync()
    air.sync()

    assert pin["id"] in silver.pins(), "absence was read as deletion"
    assert pin["id"] in air.pins()
    assert air.row(pin["id"])[3] is None, "absence became a tombstone"


# ----- FBSYNC-04: offline-first, loud, catches up ----------------------------


@pytest.mark.requirement("FBSYNC-04")
def test_an_unreachable_hub_is_loud_and_the_machine_catches_up_after(
    air: Engine, silver: Engine, hub_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the hub is down [then] sync is a loud 503 and later catches up, [else stop]."""
    monkeypatch.setenv(sync_status.ENDPOINT_ENV, f"http://127.0.0.1:{_free_port()}")
    pin = air.drop("made while the hub was down")

    failed = air.http.post("/api/v1/feedback/sync")
    assert failed.status_code == 503, failed.text
    assert failed.json()["detail"]["code"] == "FEEDBACK_SYNC_HUB_UNREACHABLE"
    assert pin["id"] in air.pins(), "the local store stopped working offline"
    offline = air.status(pin["id"])
    assert offline["cloudsync"]["last_result"] == "error"
    assert offline["pins"][0]["state"] == "pending_push"
    assert air.http.get("/api/v1/cloudsync/status").json()["last_result"]["status"] == "error"

    monkeypatch.setenv(sync_status.ENDPOINT_ENV, hub_url)
    air.sync()
    silver.sync()

    assert pin["id"] in silver.pins()
    assert air.status(pin["id"])["pins"][0]["state"] == "synced"


@pytest.mark.requirement("FBSYNC-04")
def test_status_names_an_unknown_pin_as_not_found(air: Engine) -> None:
    """[if] status names an unknown pin [then] it answers 404, [else stop]."""
    response = air.http.get("/api/v1/feedback/sync/status", params={"pin_id": "nope"})
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "COMMENT_NOT_FOUND"


@pytest.mark.requirement("FBSYNC-04")
def test_the_cli_twin_drives_a_live_engine_and_fails_loudly_without_one(
    tmp_path: Path, hub_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] the CLI targets a live engine [then] it exits 0, a dead one 1, [else stop]."""
    data_dir = tmp_path / "cli-engine"
    state_db.open_rw(sync_client.state_db_path(data_dir)).close()
    app = create_app(
        state_db_path=str(sync_client.state_db_path(data_dir)), hostname="cli-engine",
        version="9.9.9-test", mount_frontend=False, enable_cors=False,
    )
    app.state.data_dir = data_dir
    for engine_url in _serve(app, "the CLI engine"):
        assert maintenance.main(["feedback-pins", "sync", "--engine", engine_url]) == 0
        assert json.loads(capsys.readouterr().out)["status"] == "ok"
        assert maintenance.main(["feedback-pins", "status", "--engine", engine_url]) == 0
        assert "cloudsync" in json.loads(capsys.readouterr().out)
    dead = f"http://127.0.0.1:{_free_port()}"
    assert maintenance.main(["feedback-pins", "status", "--engine", dead]) == 1
    assert "[ERROR]" in capsys.readouterr().out


@pytest.mark.requirement("FBSYNC-04")
def test_the_scheduler_syncs_only_when_switched_on(air: Engine, hub_url: str) -> None:
    """[if] the switch is off [then] no thread runs; on, one tick syncs, [else stop]."""
    app = air.http.app
    off = CloudSyncScheduler(app, env={})
    off.start()
    assert not off.running, "the scheduler ran without MDT_CLOUDSYNC_SCHEDULER=1"

    on = CloudSyncScheduler(app, env={
        sync_status.SCHEDULER_ENV: "1", sync_status.ENDPOINT_ENV: hub_url,
    })
    pin = air.drop("scheduled")
    result = on.run_once()
    assert result is not None and result.status == "ok"
    assert air.status(pin["id"])["pins"][0]["state"] == "synced"


# ----- FBSYNC-05: attachment metadata syncs, the bytes gap is explicit -------


@pytest.mark.requirement("FBSYNC-05")
def test_attachment_metadata_syncs_and_the_missing_bytes_are_reported(
    air: Engine, silver: Engine
) -> None:
    """[if] a screenshot pin syncs [then] missing bytes are reported, [else stop]."""
    pin = air.drop("see screenshot")
    uploaded = air.http.post(
        f"/api/v1/feedback/comments/{pin['id']}/attachment",
        files={"file": ("shot.png", _png(), "image/png")},
    )
    assert uploaded.status_code == 201, uploaded.text
    air.sync()
    silver.sync()

    attachment = silver.pins()[pin["id"]]["attachment"]
    assert attachment == uploaded.json()["attachment"]
    fetched = silver.http.get(attachment["url"])
    assert fetched.status_code == 404
    assert fetched.json()["detail"]["code"] == "ATTACHMENT_BYTES_NOT_SYNCED"
    on_silver = silver.status(pin["id"])
    assert on_silver["pins"][0]["attachment_bytes"] == "missing"
    assert on_silver["store"]["attachments_missing"] == 1
    assert air.status(pin["id"])["pins"][0]["attachment_bytes"] == "present"
