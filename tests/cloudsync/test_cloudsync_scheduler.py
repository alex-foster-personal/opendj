"""The background CloudSync scheduler against a real hub over real HTTP.

Contract: ``apps/sync_hub/scheduler.py``. The hub is the real sync router
behind uvicorn on the loopback; each spoke is a real migrated state DB whose
scheduler talks to it through the production ``HttpTransport`` and journals
through ``maintenance.sync``. No CLI is invoked and nothing is mocked: the
only test seam is a wrapper that GATES the real sync, to force a slow round.

  - [if] two spokes do not converge in N rounds with no CLI call [then] broken, [else stop].
  - [if] a slow round lets a second round start alongside it [then] broken, [else stop].
  - [if] failures retry at the plain interval instead of backing off [then] broken, [else stop].
  - [if] stop leaves a live heartbeat or a running task [then] broken, [else stop].
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI

from apps.shared.state import db as state_db
from apps.shared.state import sync_stamp
from apps.shared.sync_runtime_gates import DEFER_REASON_PRESSURE_SHED, refuse_sync_round
from apps.sync_hub import client, maintenance, service
from apps.sync_hub import config as sync_config
from apps.sync_hub import heartbeat as sync_heartbeat
from apps.sync_hub import scheduler as sync_scheduler
from apps.sync_hub import status as sync_status
from apps.sync_hub.scheduler_owed import clear_scheduler_owed, scheduler_owed
from tests.cloudsync.conftest import free_port
from tests.waits import start_uvicorn_in_thread

pytestmark = pytest.mark.requirement("CAT-04")

#: Fast but real: every wake still re-reads the config file and beats.
_FAST = sync_scheduler.SchedulerCfg(
    INTERVAL_S=0.2,
    INITIAL_DELAY_S=0.0,
    MAX_BACKOFF_S=0.8,
    BEAT_INTERVAL_S=0.05,
    STOP_TIMEOUT_S=15.0,
)
#: Rounds a spoke may take to see a peer's edit: its own first round can run
#: before the peer pushes, so the edit arrives on round 2; one spare.
_CONVERGE_WITHIN_ROUNDS = 3
_DEADLINE_S = 30.0
_ORIGIN = "cccccccccccccccccccccccccccccccc"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("MDT_IS_HUB", sync_config.SCHEDULER_ENV, sync_config.ENDPOINT_ENV):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def live_hub(tmp_path: Path) -> Iterator[str]:
    """The real sync router on an EMPTY hub DB behind uvicorn; yields its URL."""
    hub_dir = tmp_path / "hub"
    app = FastAPI()
    app.state.state_db_path = str(client.state_db_path(hub_dir))
    app.state.sync_hub_data_dir = str(hub_dir)
    app.state.sync_hub_machine_name = "hub"
    app.include_router(service.router, prefix="/api/v1")
    port = free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server, thread = start_uvicorn_in_thread(config, what="the scheduler test hub")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)


def _spoke(tmp_path: Path, name: str, hub_url: str | None) -> Path:
    """A migrated spoke data dir, configured for ``hub_url`` when given."""
    data_dir = tmp_path / name
    data_dir.mkdir()
    state_db.open_rw(client.state_db_path(data_dir)).close()
    if hub_url is not None:
        sync_config.write_config(
            data_dir,
            sync_config.CloudSyncConfig(enabled=True, hub_url=hub_url, machine_name=name),
        )
    return data_dir


def _insert_track(data_dir: Path, stable_id: str, title: str) -> None:
    """Write one track the way an in-repo writer does (stamped and logged)."""
    conn = state_db.open_rw(client.state_db_path(data_dir))
    try:
        stamped = sync_stamp.stamp_and_log(
            conn, "tracks", (stable_id,), _ORIGIN, now=sync_stamp.canonical_now()
        ).updated_at
        conn.execute(
            "INSERT INTO tracks(stable_id, stable_id_tier, title, content_hash, "
            "created_at, updated_at, origin_device_id) "
            "VALUES (?, 'inferred', ?, ?, ?, ?, ?)",
            (
                stable_id,
                title,
                hashlib.sha256(stable_id.encode("utf-8")).hexdigest(),
                stamped,
                stamped,
                _ORIGIN,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _track_title(data_dir: Path, stable_id: str) -> str | None:
    conn = sqlite3.connect(client.state_db_path(data_dir))
    try:
        row = conn.execute("SELECT title FROM tracks WHERE stable_id = ?", (stable_id,)).fetchone()
    finally:
        conn.close()
    return None if row is None else str(row[0])


async def _until(predicate: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + _DEADLINE_S
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {_DEADLINE_S}s waiting for {what}")
        await asyncio.sleep(0.02)


def _scheduler(data_dir: Path, **kwargs: object) -> sync_scheduler.CloudSyncScheduler:
    return sync_scheduler.CloudSyncScheduler(data_dir, cfg=_FAST, env={}, **kwargs)  # type: ignore[arg-type]


# ----- convergence ----------------------------------------------------------


def test_two_spokes_converge_with_no_cli_call(tmp_path: Path, live_hub: str) -> None:
    """[if] a peer's edit misses the other spoke within N rounds [then] broken, [else stop]."""
    spoke_a = _spoke(tmp_path, "spoke-a", live_hub)
    spoke_b = _spoke(tmp_path, "spoke-b", live_hub)
    _insert_track(spoke_a, "track-1", "Written on A")
    assert _track_title(spoke_b, "track-1") is None  # control: B starts without it

    async def scenario() -> tuple[int, sync_status.CloudSyncStatus]:
        a, b = _scheduler(spoke_a), _scheduler(spoke_b)
        await a.start()
        await b.start()
        try:
            await _until(
                lambda: _track_title(spoke_b, "track-1") is not None, "B to pull A's track"
            )
            rounds_on_b = b.rounds_started
            live_status = sync_status.read_status(spoke_b, env={})
        finally:
            await a.stop()
            await b.stop()
        return rounds_on_b, live_status

    rounds_on_b, live_status = asyncio.run(scenario())

    assert _track_title(spoke_b, "track-1") == "Written on A"
    assert rounds_on_b <= _CONVERGE_WITHIN_ROUNDS
    assert (live_status.configured, live_status.running, live_status.enabled) == (True, True, True)
    for spoke in (spoke_a, spoke_b):
        journal = sync_status.read_results(spoke)
        assert journal and journal[0].status == "ok", journal
        stopped = sync_status.read_status(spoke, env={})
        assert (stopped.running, stopped.enabled) == (False, False)
        assert not sync_heartbeat.heartbeat_path(spoke).exists()


# ----- single flight ----------------------------------------------------------


def test_a_slow_round_never_overlaps_another(tmp_path: Path, live_hub: str) -> None:
    """[if] a second round starts while a slow one is in flight [then] broken, [else stop]."""
    spoke = _spoke(tmp_path, "spoke-slow", live_hub)
    first_entered, release_first = threading.Event(), threading.Event()
    guard = threading.Lock()
    active = {"now": 0, "max": 0, "calls": 0}

    def gated_sync(data_dir: Path, hub_url: str, name: str | None) -> client.SyncResult:
        with guard:
            active["now"] += 1
            active["calls"] += 1
            active["max"] = max(active["max"], active["now"])
            first_call = active["calls"] == 1
        try:
            if first_call:
                first_entered.set()
                assert release_first.wait(timeout=_DEADLINE_S), "test never released round 1"
            return maintenance.sync(data_dir, hub_url, name=name)
        finally:
            with guard:
                active["now"] -= 1

    async def scenario() -> tuple[str, bool, int]:
        scheduler = _scheduler(spoke, sync_fn=gated_sync)
        await scheduler.start()
        try:
            await asyncio.to_thread(first_entered.wait, _DEADLINE_S)
            # Ten beats and several intervals pass while round 1 is stuck.
            await asyncio.sleep(10 * _FAST.BEAT_INTERVAL_S + 2 * _FAST.INTERVAL_S)
            direct = await asyncio.to_thread(scheduler.run_round, live_hub, None)
            beating_during_round = sync_status.read_status(spoke, env={}).running
            release_first.set()
            await _until(lambda: scheduler.rounds_completed >= 3, "three completed rounds")
        finally:
            release_first.set()
            await scheduler.stop()
        return direct, beating_during_round, scheduler.busy_refusals

    direct, beating_during_round, busy_refusals = asyncio.run(scenario())

    assert direct == "busy"
    # Only the direct call above was refused: the loop itself never even
    # attempted a round while one was in flight (the lock is a second layer).
    assert busy_refusals == 1
    assert active["max"] == 1
    assert beating_during_round is True  # a long round is not a dead loop


# ----- backoff ----------------------------------------------------------------


def test_next_delay_doubles_per_failure_and_caps() -> None:
    """[if] the retry delay does not double per failure up to a cap [then] broken, [else stop]."""
    delays = [sync_scheduler.next_delay_s(300.0, n, 3600.0) for n in range(6)]
    assert delays == [300.0, 600.0, 1200.0, 2400.0, 3600.0, 3600.0]


def test_failing_rounds_back_off_and_are_journaled(tmp_path: Path) -> None:
    """[if] dead-hub rounds skip backoff or go unjournaled [then] broken, [else stop]."""
    dead_hub = f"http://127.0.0.1:{free_port()}"  # nothing listens here
    spoke = _spoke(tmp_path, "spoke-dead", dead_hub)
    starts: list[float] = []

    def timed_sync(data_dir: Path, hub_url: str, name: str | None) -> client.SyncResult:
        starts.append(time.monotonic())
        return maintenance.sync(data_dir, hub_url, name=name)

    async def scenario() -> int:
        scheduler = _scheduler(spoke, sync_fn=timed_sync)
        await scheduler.start()
        try:
            await _until(lambda: len(starts) >= 3, "three failed rounds")
        finally:
            await scheduler.stop()
        return scheduler.consecutive_failures

    failures = asyncio.run(scenario())

    assert failures >= 3
    gaps = [later - earlier for earlier, later in itertools.pairwise(starts)]
    # Lower bounds only: a sleep can overrun, it cannot undershoot.
    assert gaps[0] >= 2 * _FAST.INTERVAL_S
    assert gaps[1] >= 4 * _FAST.INTERVAL_S
    journal = sync_status.read_results(spoke)
    assert journal and all(row.status == "error" for row in journal)


# ----- config gating and lifecycle --------------------------------------------


def test_unconfigured_idles_then_picks_up_config_without_restart(
    tmp_path: Path, live_hub: str
) -> None:
    """[if] an idle scheduler syncs, beats, or ignores later config [then] broken, [else stop]."""
    spoke = _spoke(tmp_path, "spoke-late", None)

    async def scenario() -> tuple[int, bool]:
        scheduler = _scheduler(spoke)
        await scheduler.start()
        try:
            await asyncio.sleep(10 * _FAST.BEAT_INTERVAL_S)
            idle_rounds = scheduler.rounds_started
            idle_beat = sync_heartbeat.heartbeat_path(spoke).exists()
            sync_config.write_config(
                spoke,
                sync_config.CloudSyncConfig(enabled=True, hub_url=live_hub, machine_name="late"),
            )
            await _until(lambda: scheduler.rounds_completed >= 1, "a round after config appeared")
            assert sync_heartbeat.heartbeat_path(spoke).exists()
        finally:
            await scheduler.stop()
        assert not scheduler.running
        return idle_rounds, idle_beat

    idle_rounds, idle_beat = asyncio.run(scenario())

    assert (idle_rounds, idle_beat) == (0, False)
    assert not sync_heartbeat.heartbeat_path(spoke).exists()
    assert sync_status.read_results(spoke)[0].status == "ok"


def test_scheduler_defers_when_gig_posture(tmp_path: Path, live_hub: str) -> None:
    """[if] app_posture is gig [then] the scheduler skips the round and journals deferred."""
    spoke = _spoke(tmp_path, "spoke-gig", live_hub)
    prefs_dir = spoke / "state"
    prefs_dir.mkdir(parents=True, exist_ok=True)
    (prefs_dir / "ui-prefs.json").write_text('{"app_posture": "gig"}', encoding="utf-8")
    calls: list[str] = []

    def recording_sync(data_dir: Path, hub_url: str, name: str | None) -> client.SyncResult:
        calls.append(hub_url)
        return maintenance.sync(data_dir, hub_url, name=name)

    scheduler = _scheduler(spoke, sync_fn=recording_sync)
    outcome = scheduler.run_round(live_hub, "spoke-gig")

    assert outcome == "deferred"
    assert calls == []
    assert scheduler.deferred_gig == 1
    assert scheduler.deferred_deck_playing == 0
    assert scheduler.consecutive_failures == 0
    assert scheduler.rounds_started == 0
    journal = sync_status.read_results(spoke)
    assert journal and journal[0].status == "deferred"
    assert "gig_posture" in journal[0].message


def test_scheduler_defers_when_deck_playing(tmp_path: Path, live_hub: str) -> None:
    """[if] a deck is playing [then] the scheduler skips and journals deck_playing."""
    spoke = _spoke(tmp_path, "spoke-playing", live_hub)
    playing_mirror = {"decks": {"1": {"playing": True}}}
    calls: list[str] = []

    def recording_sync(data_dir: Path, hub_url: str, name: str | None) -> client.SyncResult:
        calls.append(hub_url)
        return maintenance.sync(data_dir, hub_url, name=name)

    scheduler = _scheduler(
        spoke,
        sync_fn=recording_sync,
        ui_mirror_provider=lambda: playing_mirror,
        pressure_reader=lambda: {"available": True},
    )
    outcome = scheduler.run_round(live_hub, "spoke-playing")

    assert outcome == "deferred"
    assert calls == []
    assert scheduler.deferred_deck_playing == 1
    assert scheduler.deferred_gig == 0
    assert scheduler.consecutive_failures == 0
    assert scheduler.rounds_started == 0
    journal = sync_status.read_results(spoke)
    assert journal and journal[0].status == "deferred"
    assert "deck_playing" in journal[0].message


@pytest.mark.requirement("CLOUDSYNC-09")
def test_scheduler_defers_when_pressure_elevated_and_playing(
    tmp_path: Path, live_hub: str
) -> None:
    """[if] elevated pressure and a playing deck [then] defer with pressure_shed."""
    spoke = _spoke(tmp_path, "spoke-pressure", live_hub)
    playing_mirror = {"decks": {"1": {"playing": True}}}
    elevated = {"available": True, "kernel_memory_pressure_level": 2}
    calls: list[str] = []

    def recording_sync(data_dir: Path, hub_url: str, name: str | None) -> client.SyncResult:
        calls.append(hub_url)
        return maintenance.sync(data_dir, hub_url, name=name)

    scheduler = _scheduler(
        spoke,
        sync_fn=recording_sync,
        ui_mirror_provider=lambda: playing_mirror,
        pressure_reader=lambda: elevated,
    )
    outcome = scheduler.run_round(live_hub, "spoke-pressure")

    assert outcome == "deferred"
    assert calls == []
    assert scheduler.deferred_pressure_shed == 1
    assert scheduler.deferred_deck_playing == 0
    assert scheduler.consecutive_failures == 0
    assert scheduler.rounds_started == 0
    assert scheduler_owed(spoke)
    journal = sync_status.read_results(spoke)
    assert journal and journal[0].status == "deferred"
    assert DEFER_REASON_PRESSURE_SHED in journal[0].message


@pytest.mark.requirement("CLOUDSYNC-09")
def test_scheduler_runs_owed_round_after_pressure_clears(tmp_path: Path, live_hub: str) -> None:
    """[if] owed after pressure shed and gate clears [then] sync runs once."""
    spoke = _spoke(tmp_path, "spoke-owed", live_hub)
    mirror_state = {"mirror": {"decks": {"1": {"playing": True}}}}
    elevated = {"available": True, "kernel_memory_pressure_level": 2}
    fine = {"available": True}
    pressure_state = {"payload": elevated}
    calls: list[str] = []

    def recording_sync(data_dir: Path, hub_url: str, name: str | None) -> client.SyncResult:
        calls.append(hub_url)
        return maintenance.sync(data_dir, hub_url, name=name)

    scheduler = _scheduler(
        spoke,
        sync_fn=recording_sync,
        ui_mirror_provider=lambda: mirror_state["mirror"],
        pressure_reader=lambda: pressure_state["payload"],
    )
    defer_outcome = scheduler.run_round(live_hub, "spoke-owed")
    assert defer_outcome == "deferred"
    assert scheduler_owed(spoke)

    pressure_state["payload"] = fine
    mirror_state["mirror"] = {"decks": {"1": {"playing": False}}}
    scheduler._next_due = time.monotonic() - 1.0
    run_outcome = scheduler.run_round(live_hub, "spoke-owed")

    assert run_outcome == "ok"
    assert calls == [live_hub]
    assert not scheduler_owed(spoke)
    clear_scheduler_owed(spoke)


@pytest.mark.requirement("CLOUDSYNC-09")
def test_force_bypasses_pressure_shed(tmp_path: Path) -> None:
    """[if] force is true under elevated pressure [then] refuse_sync_round allows sync."""
    mirror = {"decks": {"1": {"playing": True}}}
    pressure = {"available": True, "kernel_memory_pressure_level": 2}
    assert refuse_sync_round(tmp_path, mirror, force=True, pressure_payload=pressure) is None


def test_lifespan_never_starts_on_the_hub_and_stops_cleanly(tmp_path: Path) -> None:
    """[if] the hub gets a scheduler or exit leaves one running [then] broken, [else stop]."""
    spoke = _spoke(tmp_path, "spoke-lifespan", None)

    async def scenario() -> tuple[object, sync_scheduler.CloudSyncScheduler | None]:
        async with sync_scheduler.scheduler_lifespan(spoke, env={"MDT_IS_HUB": "1"}) as on_hub:
            pass
        async with sync_scheduler.scheduler_lifespan(spoke, env={}) as on_spoke:
            assert on_spoke is not None and on_spoke.running
        return on_hub, on_spoke

    on_hub, on_spoke = asyncio.run(scenario())

    assert on_hub is None
    assert on_spoke is not None and not on_spoke.running
