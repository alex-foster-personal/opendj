"""LIBM-128: the folder-rescan background scheduler, against a real state.db.

Contract: ``apps/engine_core/setup/folder_rescan_scheduler.py``. No CLI, no
mocked reconcile in the lifecycle tests -- only the backoff and single-flight
tests inject a test-seam ``reconcile_fn`` (the same pattern
``tests/cloudsync/test_cloudsync_scheduler.py`` uses for its ``sync_fn``),
because forcing a slow or failing REAL reconcile round deterministically
would need its own filesystem race.

  - [if] no folder import is on record [then] the scheduler never starts
    a round, [else stop].
  - [if] a folder import lands on record after boot [then] the scheduler
    picks it up with no restart, [else stop].
  - [if] a round is slow [then] a second round never starts alongside it,
    [else stop].
  - [if] rounds fail repeatedly [then] the retry delay backs off instead
    of hammering, [else stop].
  - [if] stop is called while a round is in flight [then] it is awaited,
    never abandoned, [else stop].
"""
from __future__ import annotations

import asyncio
import itertools
import threading
import time
from pathlib import Path
from unittest import mock

from apps.engine_core.setup import folder_rescan_scheduler as fr_scheduler
from apps.engine_core.setup import record as setup_record
from apps.shared.state.ingest.folder_rescan import FolderRescanReport

_FAST = fr_scheduler.FolderRescanCfg(
    INTERVAL_S=0.2,
    INITIAL_DELAY_S=0.0,
    MAX_BACKOFF_S=0.8,
    BEAT_INTERVAL_S=0.05,
    STOP_TIMEOUT_S=15.0,
)
_DEADLINE_S = 10.0


async def _until(predicate, what: str) -> None:
    deadline = time.monotonic() + _DEADLINE_S
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {_DEADLINE_S}s waiting for {what}")
        await asyncio.sleep(0.02)


def _mark_folder_import(data_dir: Path, roots: list[str]) -> None:
    setup_record.write(
        data_dir,
        setup_record.SetupRecord(dismissed=True, last_import={"kind": "folder", "roots": roots}),
    )


def _empty_report(signature: str = "sig") -> FolderRescanReport:
    return FolderRescanReport(signature=signature)


# ----- idle / config pickup ----------------------------------------------


def test_idles_with_no_folder_import_on_record(tmp_path: Path) -> None:
    calls: list[object] = []

    def counting_reconcile(writer, roots, previous_signature):
        calls.append(roots)
        return _empty_report()

    async def scenario() -> int:
        scheduler = fr_scheduler.FolderRescanScheduler(
            tmp_path, cfg=_FAST, reconcile_fn=counting_reconcile
        )
        await scheduler.start()
        try:
            await asyncio.sleep(10 * _FAST.BEAT_INTERVAL_S)
        finally:
            await scheduler.stop()
        return scheduler.rounds_started

    rounds_started = asyncio.run(scenario())

    assert rounds_started == 0
    assert calls == []


def test_a_rekordbox_only_import_does_not_start_rounds(tmp_path: Path) -> None:
    """Idle is scoped to ``kind == "folder"`` -- a rekordbox import is a
    different ingest path this scheduler does not own."""
    setup_record.write(
        tmp_path,
        setup_record.SetupRecord(dismissed=True, last_import={"kind": "rekordbox"}),
    )
    calls: list[object] = []

    def counting_reconcile(writer, roots, previous_signature):
        calls.append(roots)
        return _empty_report()

    async def scenario() -> int:
        scheduler = fr_scheduler.FolderRescanScheduler(
            tmp_path, cfg=_FAST, reconcile_fn=counting_reconcile
        )
        await scheduler.start()
        try:
            await asyncio.sleep(10 * _FAST.BEAT_INTERVAL_S)
        finally:
            await scheduler.stop()
        return scheduler.rounds_started

    assert asyncio.run(scenario()) == 0
    assert calls == []


def test_saved_watch_roots_survive_a_later_rekordbox_import(tmp_path: Path) -> None:
    setup_record.write(
        tmp_path,
        setup_record.SetupRecord(
            dismissed=True,
            last_import={"kind": "rekordbox"},
            folder_watch_roots=["/first", "/second"],
        ),
    )
    scheduler = fr_scheduler.FolderRescanScheduler(tmp_path, cfg=_FAST)

    assert scheduler._roots() == [Path("/first"), Path("/second")]


def test_state_db_open_failure_is_a_retryable_round_failure(tmp_path: Path) -> None:
    _mark_folder_import(tmp_path, [str(tmp_path / "music")])
    scheduler = fr_scheduler.FolderRescanScheduler(tmp_path, cfg=_FAST)

    with mock.patch.object(
        fr_scheduler.state_db, "open_rw", side_effect=OSError("busy")
    ):
        outcome = scheduler.run_round([tmp_path / "music"])

    assert outcome == "error"
    assert scheduler.consecutive_failures == 1
    assert scheduler.rounds_completed == 1


def test_folder_import_landing_after_boot_is_picked_up_without_restart(tmp_path: Path) -> None:
    seen_roots: list[list[Path]] = []

    def recording_reconcile(writer, roots, previous_signature):
        seen_roots.append(roots)
        return _empty_report()

    async def scenario() -> None:
        scheduler = fr_scheduler.FolderRescanScheduler(
            tmp_path, cfg=_FAST, reconcile_fn=recording_reconcile
        )
        await scheduler.start()
        try:
            await asyncio.sleep(5 * _FAST.BEAT_INTERVAL_S)
            assert scheduler.rounds_started == 0
            _mark_folder_import(tmp_path, ["/music/root"])
            await _until(lambda: scheduler.rounds_started >= 1, "a round after config appeared")
        finally:
            await scheduler.stop()

    asyncio.run(scenario())

    assert seen_roots and seen_roots[0] == [Path("/music/root")]


# ----- single flight -------------------------------------------------------


def test_a_slow_round_never_overlaps_another(tmp_path: Path) -> None:
    _mark_folder_import(tmp_path, [str(tmp_path / "music")])
    first_entered, release_first = threading.Event(), threading.Event()
    guard = threading.Lock()
    active = {"now": 0, "max": 0, "calls": 0}

    def gated_reconcile(writer, roots, previous_signature):
        with guard:
            active["now"] += 1
            active["calls"] += 1
            active["max"] = max(active["max"], active["now"])
            first_call = active["calls"] == 1
        try:
            if first_call:
                first_entered.set()
                assert release_first.wait(timeout=_DEADLINE_S), "test never released round 1"
            return _empty_report(signature=f"sig-{active['calls']}")
        finally:
            with guard:
                active["now"] -= 1

    async def scenario() -> None:
        scheduler = fr_scheduler.FolderRescanScheduler(
            tmp_path, cfg=_FAST, reconcile_fn=gated_reconcile
        )
        await scheduler.start()
        try:
            await asyncio.to_thread(first_entered.wait, _DEADLINE_S)
            await asyncio.sleep(10 * _FAST.BEAT_INTERVAL_S + 2 * _FAST.INTERVAL_S)
            release_first.set()
            await _until(lambda: scheduler.rounds_completed >= 3, "three completed rounds")
        finally:
            release_first.set()
            await scheduler.stop()

    asyncio.run(scenario())

    assert active["max"] == 1


# ----- backoff ---------------------------------------------------------------


def test_failing_rounds_back_off(tmp_path: Path) -> None:
    _mark_folder_import(tmp_path, [str(tmp_path / "music")])
    starts: list[float] = []

    def failing_reconcile(writer, roots, previous_signature):
        starts.append(time.monotonic())
        raise RuntimeError("simulated reconcile failure")

    async def scenario() -> int:
        scheduler = fr_scheduler.FolderRescanScheduler(
            tmp_path, cfg=_FAST, reconcile_fn=failing_reconcile
        )
        await scheduler.start()
        try:
            await _until(lambda: len(starts) >= 3, "three failed rounds")
        finally:
            await scheduler.stop()
        return scheduler.consecutive_failures

    failures = asyncio.run(scenario())

    assert failures >= 3
    gaps = [later - earlier for earlier, later in itertools.pairwise(starts)]
    assert gaps[0] >= 2 * _FAST.INTERVAL_S
    assert gaps[1] >= 4 * _FAST.INTERVAL_S


def test_a_success_after_failures_resets_the_backoff(tmp_path: Path) -> None:
    _mark_folder_import(tmp_path, [str(tmp_path / "music")])
    state = {"calls": 0}

    def flaky_reconcile(writer, roots, previous_signature):
        state["calls"] += 1
        if state["calls"] <= 2:
            raise RuntimeError("simulated reconcile failure")
        return _empty_report()

    async def scenario() -> int:
        scheduler = fr_scheduler.FolderRescanScheduler(
            tmp_path, cfg=_FAST, reconcile_fn=flaky_reconcile
        )
        await scheduler.start()
        try:
            await _until(lambda: scheduler.rounds_completed >= 3, "recovery round")
        finally:
            await scheduler.stop()
        return scheduler.consecutive_failures

    failures_after_recovery = asyncio.run(scenario())

    assert failures_after_recovery == 0


# ----- lifecycle / drain -----------------------------------------------------


def test_stop_awaits_an_in_flight_round_rather_than_abandoning_it(tmp_path: Path) -> None:
    _mark_folder_import(tmp_path, [str(tmp_path / "music")])
    entered, may_finish = threading.Event(), threading.Event()

    def slow_reconcile(writer, roots, previous_signature):
        entered.set()
        assert may_finish.wait(timeout=_DEADLINE_S), "test never released the round"
        return _empty_report()

    finished = {"value": False}

    async def scenario() -> None:
        scheduler = fr_scheduler.FolderRescanScheduler(
            tmp_path, cfg=_FAST, reconcile_fn=slow_reconcile
        )
        await scheduler.start()
        await asyncio.to_thread(entered.wait, _DEADLINE_S)

        async def do_stop() -> None:
            await scheduler.stop()
            finished["value"] = True

        stop_task = asyncio.create_task(do_stop())
        await asyncio.sleep(0.1)
        assert finished["value"] is False, "stop must wait, not abandon, the in-flight round"
        may_finish.set()
        await stop_task

    asyncio.run(scenario())

    assert finished["value"] is True


def test_lifespan_starts_and_stops_cleanly(tmp_path: Path) -> None:
    async def scenario() -> tuple[bool, bool]:
        async with fr_scheduler.folder_rescan_lifespan(tmp_path) as scheduler:
            running_inside = scheduler.running
        return running_inside, scheduler.running

    running_inside, running_after = asyncio.run(scenario())

    assert running_inside is True
    assert running_after is False


def test_status_reports_the_last_report_after_a_real_round(tmp_path: Path) -> None:
    """Real reconcile, real (empty) state.db, real folder: proves ``run_round``
    wires ``reconcile_fn`` -> ``state_db.open_rw`` -> ``.status()`` end to end."""
    root = tmp_path / "music"
    root.mkdir()
    _mark_folder_import(tmp_path, [str(root)])

    scheduler = fr_scheduler.FolderRescanScheduler(tmp_path, cfg=_FAST)
    outcome = scheduler.run_round([root])

    assert outcome == "ok"
    status = scheduler.status()
    assert status["warning"] is None
    assert status["last_cycle_at"] is not None
    assert status["tracks_added_last_cycle"] == 0
    assert (tmp_path / "state" / "state.db").exists()


def test_a_denied_root_warning_is_logged_not_just_recorded(
    tmp_path: Path, caplog
) -> None:
    """AC: 'fails loudly, never silently'. A field on ``report.warning`` that
    nobody polls is silent in practice, so ``run_round`` must also emit a log
    line -- the channel that reaches a process's own logs/alerting without
    requiring a caller to poll ``/setup/status`` first."""
    root = tmp_path / "music"
    _mark_folder_import(tmp_path, [str(root)])

    def denied_reconcile(writer, roots, previous_signature):
        return FolderRescanReport(signature="sig", warning="could not read /music/root: denied")

    scheduler = fr_scheduler.FolderRescanScheduler(
        tmp_path, cfg=_FAST, reconcile_fn=denied_reconcile
    )
    with caplog.at_level("WARNING", logger=fr_scheduler.log.name):
        outcome = scheduler.run_round([root])

    assert outcome == "ok"
    assert any("could not read /music/root: denied" in record.message for record in caplog.records)


def test_a_malformed_setup_record_idles_rather_than_crashing_the_loop(tmp_path: Path) -> None:
    (tmp_path / "setup.json").write_text("not json", encoding="utf-8")

    async def scenario() -> int:
        scheduler = fr_scheduler.FolderRescanScheduler(tmp_path, cfg=_FAST)
        await scheduler.start()
        try:
            await asyncio.sleep(10 * _FAST.BEAT_INTERVAL_S)
        finally:
            await scheduler.stop()
        return scheduler.rounds_started

    assert asyncio.run(scenario()) == 0
