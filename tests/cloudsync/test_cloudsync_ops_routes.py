"""HTTP twins of the CloudSync operator CLI (plan W4): sync, fleet, grant.

Every test drives the real webui app (``create_app``) over the real ASGI
stack, against the real hub fixtures in ``conftest.py`` -- the live uvicorn
hub where a request has to cross a socket. Nothing is patched.

Regression one-liners:
  - [if] POST /sync skips the maintenance.sync journal [then] /status misses it, [else stop]
  - [if] POST /sync to a dead hub is not 502 plus an error entry [then] broken, [else stop]
  - [if] GET /fleet differs from `fleet --json` in any key or value [then] broken, [else stop]
  - [if] a spoke (MDT_IS_HUB unset) can mint a grant [then] broken, [else stop]
  - [if] an unknown owner email gets a grant or a grant row [then] broken, [else stop]
  - [if] an HTTP-minted grant does not redeem via the enroll CLI [then] broken, [else stop]
  - [if] the grant token reaches any log record [then] broken, [else stop]
  - [if] a non-loopback, share or Tailscale Serve caller gets in [then] broken, [else stop]
  - [if] a loopback peer with a rebound Host or an X-Forwarded-For gets in [then] broken
  - [if] a held run lock or a digest mismatch is not a declared 409 [then] broken
  - [if] Sync now runs while a scheduler round holds the data dir [then] broken, [else stop]
  - [if] a refused PUT /cloudsync/config writes the config file [then] broken, [else stop]
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state import schema as state_schema
from apps.sync_hub import client, enrollment_credentials, maintenance
from apps.sync_hub import config as sync_config
from apps.sync_hub.scheduler import CloudSyncScheduler
from apps.sync_hub.scheduler_owed import owed_path
from apps.sync_hub.single_flight import sync_flock_for, sync_lock_for
from apps.webui.server.app import create_app
from apps.webui.server.local_operator import is_loopback_ip
from apps.webui.server.share_gate import AUTH_TOKEN, ShareConfig
from apps.webui.server.sqlite_backend import SqliteBackend
from tests.cloudsync.conftest import (
    ENROLL_OWNER_EMAIL,
    ENROLL_OWNER_SUB,
    HELLO_PATH,
    free_port,
)
from tests.cloudsync.enrollment_helpers import (
    http_enroll,
    machine_payload,
    mint_grant,
    owner_rows,
    read_hub,
)
from tests.cloudsync.enrollment_transport import TestClientTransport
from tests.cloudsync.test_hub_sync import _DEV_A, _T0, _insert_track, _open

pytestmark = pytest.mark.requirement("CAT-04")

LOOPBACK_CLIENT: tuple[str, int] = ("127.0.0.1", 50123)
#: What a browser on the machine sends as Host; TestClient's default
#: ``testserver`` is not a loopback authority and the guard refuses it.
LOOPBACK_BASE_URL: str = "http://127.0.0.1:8686"
SHARE_HOST: str = "share.example.test"
SHARE_TOKEN: str = "share-token-for-tests"


@contextmanager
def ops_client(
    data_dir: Path,
    *,
    client_addr: tuple[str, int] = LOOPBACK_CLIENT,
    share_config: ShareConfig | None = None,
) -> Iterator[TestClient]:
    """The real webui app over ``data_dir``'s state DB, called from ``client_addr``
    with a loopback ``Host``."""
    db_path = client.state_db_path(data_dir)
    state_db.open_rw(db_path, apply_schema=True).close()
    app = create_app(
        backend=SqliteBackend(db_path),
        bind_host="127.0.0.1",
        hostname="cloudsync-ops-test",
        state_db_path=str(db_path),
        mount_frontend=False,
        port=8686,
        frontend_port=5173,
        share_config=share_config,
    )
    with TestClient(app, base_url=LOOPBACK_BASE_URL, client=client_addr) as http:
        yield http


def _hub_track_title(hub_dir: Path, stable_id: str) -> str | None:
    conn = _open(hub_dir)
    try:
        row = conn.execute("SELECT title FROM tracks WHERE stable_id = ?", (stable_id,)).fetchone()
        return None if row is None else str(row[0])
    finally:
        conn.close()


def _mint_over_http(hub_dir: Path, email: str = ENROLL_OWNER_EMAIL) -> Any:
    with ops_client(hub_dir) as http:
        return http.post("/api/v1/cloudsync/enrollment-grants", json={"owner_email": email})


# ----- POST /sync ----------------------------------------------------------


def test_post_sync_journals_a_result_that_status_shows(
    enroll_live_hub: str, enroll_hub_dir: Path, enroll_spoke_dir: Path
) -> None:
    """if POST /sync does not run a real round trip journaled for GET /status then broken"""
    conn = _open(enroll_spoke_dir)
    try:
        _insert_track(conn, "trk-1", title="one", updated_at=_T0, origin=_DEV_A)
    finally:
        conn.close()

    with ops_client(enroll_spoke_dir) as http:
        before = http.get("/api/v1/cloudsync/status").json()
        response = http.post(
            "/api/v1/cloudsync/sync",
            json={"hub_url": enroll_live_hub, "name": "spoke-a"},
        )
        after = http.get("/api/v1/cloudsync/status").json()

    assert before["recent_results"] == [], "control: the journal starts empty"
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["accepted"] == 1 and body["pushed"] == 1
    assert body["digest_inconclusive"] is False
    assert _hub_track_title(enroll_hub_dir, "trk-1") == "one", "the HTTP sync did not reach the hub"
    assert after["last_result"]["status"] == "ok"
    newest = after["recent_results"][0]
    assert (newest["pushed"], newest["pulled"]) == (body["pushed"], body["pulled"])
    assert len(after["recent_results"]) == 1


def test_post_sync_to_an_unreachable_hub_is_502_and_journaled_as_error(
    enroll_spoke_dir: Path,
) -> None:
    """if a sync against a dead hub is not a declared 502 with an error journal entry then broken"""
    dead_hub = f"http://127.0.0.1:{free_port()}"
    with ops_client(enroll_spoke_dir) as http:
        response = http.post("/api/v1/cloudsync/sync", json={"hub_url": dead_hub})
        status = http.get("/api/v1/cloudsync/status").json()

    assert response.status_code == 502, response.text
    assert response.json()["detail"]["code"] == "CLOUDSYNC_HUB_UNREACHABLE"
    assert status["last_result"]["status"] == "error"


def test_post_sync_maps_a_declared_refusal_to_409_and_journals_it(
    enroll_live_hub: str, enroll_spoke_dir: Path
) -> None:
    """if run_sync's declared refusal (a real digest mismatch) is not a journaled 409 then broken"""
    conn = _open(enroll_spoke_dir)
    try:
        _insert_track(conn, "trk-1", title="one", updated_at=_T0, origin=_DEV_A)
    finally:
        conn.close()

    with ops_client(enroll_spoke_dir) as http:
        first = http.post("/api/v1/cloudsync/sync", json={"hub_url": enroll_live_hub})
        conn = _open(enroll_spoke_dir)
        try:
            # A raw write bypasses the stamp helper, so the push cannot see it
            # and the digest compare is the backstop that has to fire.
            conn.execute("UPDATE tracks SET title = ? WHERE stable_id = ?", ("bypassed", "trk-1"))
        finally:
            conn.close()
        refused = http.post("/api/v1/cloudsync/sync", json={"hub_url": enroll_live_hub})
        status = http.get("/api/v1/cloudsync/status").json()

    assert first.status_code == 200, "control: the first round is clean"
    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert detail["code"] == "CLOUDSYNC_SYNC_REFUSED"
    assert detail["message"].startswith("SyncDigestMismatch")
    assert [entry["status"] for entry in status["recent_results"]] == ["error", "ok"]


def test_post_sync_returns_409_when_gig_posture(
    enroll_live_hub: str, enroll_spoke_dir: Path
) -> None:
    """[if] app_posture is gig [then] POST /sync returns 409 CLOUDSYNC_SYNC_DEFERRED."""
    prefs_dir = enroll_spoke_dir / "state"
    prefs_dir.mkdir(parents=True, exist_ok=True)
    (prefs_dir / "ui-prefs.json").write_text('{"app_posture": "gig"}', encoding="utf-8")

    with ops_client(enroll_spoke_dir) as http:
        response = http.post(
            "/api/v1/cloudsync/sync",
            json={"hub_url": enroll_live_hub, "force": False},
        )
        status = http.get("/api/v1/cloudsync/status").json()

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "CLOUDSYNC_SYNC_DEFERRED"
    assert "gig_posture" in detail["message"]
    assert status["recent_results"] == []


def test_post_sync_force_true_completes_under_gig_posture(
    enroll_live_hub: str, enroll_spoke_dir: Path
) -> None:
    """[if] force=true under gig posture [then] POST /sync completes and journals."""
    prefs_dir = enroll_spoke_dir / "state"
    prefs_dir.mkdir(parents=True, exist_ok=True)
    (prefs_dir / "ui-prefs.json").write_text('{"app_posture": "gig"}', encoding="utf-8")

    with ops_client(enroll_spoke_dir) as http:
        response = http.post(
            "/api/v1/cloudsync/sync",
            json={"hub_url": enroll_live_hub, "force": True},
        )
        status = http.get("/api/v1/cloudsync/status").json()

    assert response.status_code == 200, response.text
    assert len(status["recent_results"]) == 1
    assert status["last_result"]["status"] in {"ok", "inconclusive"}


def test_post_sync_force_true_still_busy_when_lock_held(enroll_spoke_dir: Path) -> None:
    """[if] force=true while sync lock is held [then] 409 CLOUDSYNC_SYNC_IN_PROGRESS."""
    lock = sync_lock_for(enroll_spoke_dir)
    with ops_client(enroll_spoke_dir) as http:
        assert lock.acquire(blocking=False), "control: nothing else holds the lock"
        try:
            response = http.post(
                "/api/v1/cloudsync/sync",
                json={"hub_url": "http://127.0.0.1:9", "force": True},
            )
        finally:
            lock.release()
        status = http.get("/api/v1/cloudsync/status").json()

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "CLOUDSYNC_SYNC_IN_PROGRESS"
    assert status["recent_results"] == []


def test_post_sync_is_409_not_500_when_a_standalone_cli_holds_the_flock(
    enroll_spoke_dir: Path,
) -> None:
    """[if] a standalone CLI process holds the cross-process sync flock when
    ``POST /sync`` calls in [then] the route answers 409 CLOUDSYNC_SYNC_IN_PROGRESS,
    not an uncaught 500 (Codex review, PR #3831, P2/NON-BLOCKING).

    Distinct from `test_post_sync_force_true_still_busy_when_lock_held`
    above: that test holds ``sync_lock_for`` (the IN-PROCESS lock
    ``_one_sync_at_a_time`` checks BEFORE ever calling ``maintenance.sync``)
    to prove the pre-check. This test leaves that in-process lock free and
    holds ONLY ``sync_flock_for`` (the cross-process ``fcntl.flock`` a
    standalone CLI in another process would hold), so the pre-check passes
    and ``maintenance.sync`` itself is the one that hits contention and
    raises ``SyncDeferredError`` -- proving the route's OWN try/except
    around that call, not the earlier gate.
    """
    with ops_client(enroll_spoke_dir) as http:
        with sync_flock_for(enroll_spoke_dir):
            response = http.post(
                "/api/v1/cloudsync/sync",
                json={"hub_url": "http://127.0.0.1:9", "force": True},
            )
        status = http.get("/api/v1/cloudsync/status").json()

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "CLOUDSYNC_SYNC_IN_PROGRESS"
    assert status["recent_results"] == []


def test_post_sync_returns_409_when_deck_playing(
    enroll_live_hub: str, enroll_spoke_dir: Path
) -> None:
    """[if] a deck is playing [then] POST /sync returns 409 CLOUDSYNC_SYNC_DEFERRED."""
    with ops_client(enroll_spoke_dir) as http:
        http.app.state.ui_mirror = {"decks": {"1": {"playing": True}}}
        http.app.state.machine_pressure = {"available": True}
        response = http.post("/api/v1/cloudsync/sync", json={"hub_url": enroll_live_hub})
        status = http.get("/api/v1/cloudsync/status").json()

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "CLOUDSYNC_SYNC_DEFERRED"
    assert "deck_playing" in detail["message"]
    assert status["recent_results"] == []


def test_post_sync_does_not_refuse_when_mirror_absent_and_prep(
    enroll_live_hub: str, enroll_spoke_dir: Path
) -> None:
    """[if] prep posture and no mirror [then] POST /sync still runs."""
    with ops_client(enroll_spoke_dir) as http:
        assert getattr(http.app.state, "ui_mirror", None) is None
        response = http.post(
            "/api/v1/cloudsync/sync",
            json={"hub_url": enroll_live_hub, "name": "spoke-a"},
        )

    assert response.status_code == 200, response.text


def test_post_sync_refuses_a_second_run_in_the_same_process(enroll_spoke_dir: Path) -> None:
    """if a sync starts while the run lock is held, or that refusal is journaled, then broken"""
    lock = sync_lock_for(enroll_spoke_dir)
    with ops_client(enroll_spoke_dir) as http:
        assert lock.acquire(blocking=False), "control: nothing else holds the lock"
        try:
            response = http.post("/api/v1/cloudsync/sync", json={"hub_url": "http://127.0.0.1:9"})
        finally:
            lock.release()
        status = http.get("/api/v1/cloudsync/status").json()

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "CLOUDSYNC_SYNC_IN_PROGRESS"
    assert status["recent_results"] == [], "refused before syncing, so nothing is journaled"


def test_sync_now_is_refused_while_a_scheduler_round_is_in_flight(
    enroll_live_hub: str, enroll_spoke_dir: Path
) -> None:
    """if Sync now can run alongside a scheduler round on one state.db then broken"""
    round_entered, release_round = threading.Event(), threading.Event()

    def gated_sync(data_dir: Path, hub_url: str, name: str | None) -> client.SyncResult:
        round_entered.set()
        assert release_round.wait(timeout=30.0), "test never released the round"
        return maintenance.sync(data_dir, hub_url, name=name)

    scheduler = CloudSyncScheduler(enroll_spoke_dir, sync_fn=gated_sync, env={})
    outcome: dict[str, str] = {}
    worker = threading.Thread(
        target=lambda: outcome.update(round=scheduler.run_round(enroll_live_hub, "spoke-a"))
    )
    worker.start()
    try:
        assert round_entered.wait(timeout=30.0), "control: the scheduler round started"
        with ops_client(enroll_spoke_dir) as http:
            during = http.post("/api/v1/cloudsync/sync", json={"hub_url": enroll_live_hub})
    finally:
        release_round.set()
        worker.join(timeout=30.0)
    with ops_client(enroll_spoke_dir) as http:
        after = http.post("/api/v1/cloudsync/sync", json={"hub_url": enroll_live_hub})

    assert during.status_code == 409, during.text
    assert during.json()["detail"]["code"] == "CLOUDSYNC_SYNC_IN_PROGRESS"
    assert outcome == {"round": "ok"}, "control: the held round itself completed"
    assert after.status_code == 200, f"control: the lock is released after the round: {after.text}"


def test_a_scheduler_round_is_busy_while_the_sync_lock_is_held(enroll_spoke_dir: Path) -> None:
    """if a scheduler round ignores the lock Sync now holds then broken"""
    calls: list[str] = []

    def recording_sync(data_dir: Path, hub_url: str, name: str | None) -> client.SyncResult:
        calls.append(hub_url)
        return maintenance.sync(data_dir, hub_url, name=name)

    scheduler = CloudSyncScheduler(enroll_spoke_dir, sync_fn=recording_sync, env={})
    lock = sync_lock_for(enroll_spoke_dir)
    assert lock.acquire(blocking=False), "control: nothing else holds the lock"
    try:
        held = scheduler.run_round(f"http://127.0.0.1:{free_port()}", None)
    finally:
        lock.release()
    released = scheduler.run_round(f"http://127.0.0.1:{free_port()}", None)

    assert held == "busy"
    assert released == "error", "control: with the lock free the round runs (a dead hub errors)"
    assert len(calls) == 1, "only the unlocked round reached the sync"


def test_a_scheduler_round_is_busy_when_a_standalone_cli_holds_the_flock(
    enroll_spoke_dir: Path,
) -> None:
    """[if] a standalone CLI process holds the cross-process sync flock while
    a scheduler round starts [then] the round outcome is "busy" without
    growing the failure backoff, not "error" (Codex review, PR #3831,
    P2/NON-BLOCKING).

    Distinct from `test_a_scheduler_round_is_busy_while_the_sync_lock_is_held`
    above: that test holds ``sync_lock_for`` (the scheduler's own IN-PROCESS
    round lock, checked by ``run_round`` BEFORE ``_sync_once`` is ever
    called). This test leaves that lock free and holds ONLY
    ``single_flight.sync_flock_for`` (the cross-process ``fcntl.flock`` a
    standalone CLI in another process would hold), so ``run_round`` proceeds
    into ``_sync_once`` -- the real ``maintenance.sync`` (the scheduler's own
    default ``sync_fn``, unmocked) is the one that hits the contention, which
    is the code path this fix targets.
    """
    scheduler = CloudSyncScheduler(enroll_spoke_dir, env={})
    with sync_flock_for(enroll_spoke_dir):
        outcome = scheduler.run_round(f"http://127.0.0.1:{free_port()}", None)

    assert outcome == "busy"
    assert scheduler.busy_refusals == 1
    assert scheduler.consecutive_failures == 0, (
        "an expected flock busy-skip must not grow the failure backoff"
    )


def test_a_refused_config_put_writes_nothing(enroll_spoke_dir: Path) -> None:
    """if a tailnet peer's PUT /cloudsync/config reaches the file then broken"""
    body = {"enabled": True, "hub_url": "http://attacker.example:8686", "machine_name": None}
    with ops_client(enroll_spoke_dir, client_addr=("203.0.113.7", 50123)) as http:
        refused = http.put("/api/v1/cloudsync/config", json=body)
    stored_after_refusal = sync_config.read_config(enroll_spoke_dir)
    with ops_client(enroll_spoke_dir) as http:
        allowed = http.put("/api/v1/cloudsync/config", json=body)

    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"]["code"] == "CLOUDSYNC_OPS_LOCAL_ONLY"
    assert stored_after_refusal is None
    assert allowed.status_code == 200, f"control: the loopback operator can write: {allowed.text}"
    assert sync_config.read_config(enroll_spoke_dir) == sync_config.CloudSyncConfig(**body)


# ----- GET /fleet ----------------------------------------------------------


def test_get_fleet_equals_the_cli_fleet_json(
    enroll_hub: TestClientTransport,
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
    enroll_other_spoke_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """if GET /fleet and `fleet --json` differ in any key or value then broken"""
    enroll_hub.post(
        HELLO_PATH,
        {
            "machine": machine_payload(enroll_other_spoke_dir, name="never-enrolled"),
            "schema_version": state_schema.SCHEMA_VERSION,
            "machines": [],
        },
    )
    http_enroll(enroll_hub, enroll_spoke_dir, name="nucbox-wsl", token=mint_grant(enroll_hub_dir))

    with ops_client(enroll_hub_dir) as http:
        response = http.get("/api/v1/cloudsync/fleet")
    capsys.readouterr()
    assert maintenance.main(["fleet", "--data-dir", str(enroll_hub_dir), "--json"]) == 0
    cli_payload = json.loads(capsys.readouterr().out)

    assert response.status_code == 200, response.text
    assert cli_payload["owned"] == 1 and cli_payload["unowned"] >= 1, (
        "control: the readout under comparison must hold both states"
    )
    assert json.dumps(response.json(), sort_keys=True) == json.dumps(cli_payload, sort_keys=True)


# ----- POST /enrollment-grants ----------------------------------------------


def test_a_spoke_refuses_to_mint_a_grant(enroll_hub_dir: Path) -> None:
    """if a machine without MDT_IS_HUB=1 can mint a grant over HTTP then broken"""
    response = _mint_over_http(enroll_hub_dir)

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "CLOUDSYNC_NOT_A_HUB"
    assert read_hub(enroll_hub_dir, "enrollment_grants") == []


def test_an_unreadable_hub_flag_is_a_declared_500(
    enroll_hub_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """if MDT_IS_HUB=yes is read as hub or spoke instead of a declared 500 then broken"""
    monkeypatch.setenv("MDT_IS_HUB", "yes")
    response = _mint_over_http(enroll_hub_dir)

    assert response.status_code == 500, response.text
    assert response.json()["detail"]["code"] == "CLOUDSYNC_HUB_FLAG_INVALID"
    assert read_hub(enroll_hub_dir, "enrollment_grants") == []


def test_an_unknown_owner_gets_no_grant(
    enroll_hub_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """if a grant can be minted for an email with no users row on the hub then broken"""
    monkeypatch.setenv("MDT_IS_HUB", "1")
    response = _mint_over_http(enroll_hub_dir, "nobody@example.com")

    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "CLOUDSYNC_GRANT_OWNER_UNKNOWN"
    assert read_hub(enroll_hub_dir, "enrollment_grants") == []


def test_a_grant_minted_over_http_redeems_through_the_enroll_cli(
    enroll_live_hub: str,
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """if an HTTP-minted grant does not enroll a machine through the real enroll CLI then broken"""
    monkeypatch.setenv("MDT_IS_HUB", "1")
    response = _mint_over_http(enroll_hub_dir)
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    token = response.json()["token"]
    stored = read_hub(enroll_hub_dir, "enrollment_grants")
    assert [row["grant_token_sha256"] for row in stored] == [
        enrollment_credentials.hash_grant_token(token)
    ], "only the hash is stored, and it is this token's"

    monkeypatch.delenv("MDT_IS_HUB")  # the joining machine is a spoke
    grant_file = tmp_path / "grant.txt"
    grant_file.write_text(token, encoding="utf-8")
    capsys.readouterr()
    code = maintenance.main(
        [
            "enroll",
            "--data-dir",
            str(enroll_spoke_dir),
            "--hub",
            enroll_live_hub,
            "--name",
            "nucbox-wsl",
            "--grant-file",
            str(grant_file),
        ]
    )

    assert code == 0
    assert capsys.readouterr().out.startswith("enrolled nucbox-wsl")
    owners = owner_rows(enroll_hub_dir)
    assert [(row["google_sub"], row["enrolled_via"]) for row in owners] == [
        (ENROLL_OWNER_SUB, "grant")
    ]


def test_the_grant_token_is_never_logged(
    enroll_hub_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """if the raw grant token reaches any log record then broken"""
    monkeypatch.setenv("MDT_IS_HUB", "1")
    caplog.set_level(logging.DEBUG)
    response = _mint_over_http(enroll_hub_dir)
    token = response.json()["token"]

    assert f"minted enrollment grant for {ENROLL_OWNER_EMAIL}" in caplog.text, (
        "control: the capture sees this route's own audit line"
    )
    assert token not in caplog.text


@pytest.mark.requirement("CLOUDSYNC-09")
def test_post_scheduler_resume_owed_creates_marker(enroll_spoke_dir: Path) -> None:
    """if POST /scheduler/resume-owed does not mark owed [then] broken."""
    with ops_client(enroll_spoke_dir) as http:
        response = http.post("/api/v1/cloudsync/scheduler/resume-owed")
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True}
    assert owed_path(enroll_spoke_dir).is_file()


# ----- local-operator guard -------------------------------------------------

OPS_CALLS: list[tuple[str, str, dict[str, Any] | None]] = [
    ("POST", "/api/v1/cloudsync/scheduler/resume-owed", None),
    ("POST", "/api/v1/cloudsync/sync", {"hub_url": "http://127.0.0.1:9"}),
    ("GET", "/api/v1/cloudsync/fleet", None),
    ("POST", "/api/v1/cloudsync/enrollment-grants", {"owner_email": ENROLL_OWNER_EMAIL}),
    # PUT repoints this machine's scheduler at a hub; GET discloses the hub URL.
    (
        "PUT",
        "/api/v1/cloudsync/config",
        {"enabled": True, "hub_url": "http://attacker.example:8686", "machine_name": None},
    ),
    ("GET", "/api/v1/cloudsync/config", None),
]


REBIND_HOST: str = "rebind.attacker.example:8686"

#: Requests from a LOOPBACK peer that the guard must still refuse.
LOOPBACK_REFUSALS: dict[str, dict[str, str]] = {
    "share host": {"host": SHARE_HOST, "authorization": f"Bearer {SHARE_TOKEN}"},
    "tailscale serve user": {"tailscale-user-login": "x@example.com"},
    "tailscale serve tagged (xff only)": {"x-forwarded-for": "203.0.113.7"},
    "rfc 7239 forwarded": {"forwarded": "for=203.0.113.7"},
    "dns rebinding": {
        "host": REBIND_HOST,
        "origin": f"http://{REBIND_HOST}",
        "sec-fetch-site": "same-origin",
    },
    "foreign origin": {"origin": "https://evil.example"},
    "null origin": {"origin": "null"},
    "cross-site fetch": {"sec-fetch-site": "cross-site"},
}

#: Loopback requests the operator really makes; each must NOT be refused.
LOOPBACK_CONTROLS: dict[str, dict[str, str]] = {
    "curl / agent": {},
    "sveltekit dev ui": {
        "host": "localhost:8686",
        "origin": "http://localhost:5173",
        "sec-fetch-site": "same-site",
    },
    "ipv6 loopback host": {"host": "[::1]:8686", "origin": "http://[::1]:8686"},
}


@pytest.mark.parametrize(("method", "path", "payload"), OPS_CALLS)
def test_operator_routes_refuse_callers_that_are_not_the_local_operator(
    enroll_hub_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    payload: dict[str, Any] | None,
) -> None:
    """if a remote, proxied, rebound, foreign-origin or cross-site caller gets in then broken"""
    monkeypatch.setenv("MDT_IS_HUB", "1")
    share = ShareConfig(host=SHARE_HOST, auth=AUTH_TOKEN, token=SHARE_TOKEN, read_only=False)
    refused: dict[str, Any] = {}
    allowed: dict[str, Any] = {}
    with ops_client(enroll_hub_dir, client_addr=("203.0.113.7", 50123)) as http:
        refused["tailnet peer"] = http.request(method, path, json=payload)
    with ops_client(enroll_hub_dir, client_addr=("::ffff:203.0.113.7", 50123)) as http:
        refused["ipv4-mapped tailnet peer"] = http.request(method, path, json=payload)
    with ops_client(enroll_hub_dir, client_addr=("::ffff:127.0.0.1", 50123)) as http:
        allowed["ipv4-mapped loopback peer"] = http.request(method, path, json=payload)
    with ops_client(enroll_hub_dir, share_config=share) as http:
        for label, headers in LOOPBACK_REFUSALS.items():
            refused[label] = http.request(method, path, json=payload, headers=headers)
        for label, headers in LOOPBACK_CONTROLS.items():
            allowed[label] = http.request(method, path, json=payload, headers=headers)

    assert {label: r.status_code for label, r in refused.items()} == dict.fromkeys(refused, 403)
    refused_codes = {
        "CLOUDSYNC_OPS_LOCAL_ONLY",
        "HOST_NOT_ALLOWED",
        "ORIGIN_NOT_ALLOWED",
    }

    def _refusal_code(response) -> str:
        body = response.json()
        if "detail" in body:
            return body["detail"]["code"]
        return body["code"]

    assert {_refusal_code(r) for r in refused.values()}.issubset(refused_codes)
    assert all(r.status_code != 403 for r in allowed.values()), {
        label: r.text for label, r in allowed.items() if r.status_code == 403
    }
    minted_by_controls = len(allowed) if path.endswith("/enrollment-grants") else 0
    assert len(read_hub(enroll_hub_dir, "enrollment_grants")) == minted_by_controls, (
        "the refused calls minted nothing; only the loopback controls did"
    )


@pytest.mark.parametrize(
    ("peer", "expected"),
    [
        ("127.0.0.1", True),
        ("127.0.0.2", True),
        ("::1", True),
        ("::ffff:127.0.0.1", True),
        ("localhost", False),
        ("203.0.113.7", False),
        ("::ffff:203.0.113.7", False),
        ("testclient", False),
        (None, False),
    ],
)
def test_loopback_peer_classification(peer: str | None, expected: bool) -> None:
    """if a peer address is misclassified as loopback or not then broken"""
    assert is_loopback_ip(peer) is expected
