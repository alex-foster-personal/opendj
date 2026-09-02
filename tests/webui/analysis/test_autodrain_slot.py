"""What the reconcile loop must survive when the one job slot changes hands.

Split out of test_autodrain_booking.py, which owns what a tick may ADOPT.
This module owns the narrower fact underneath every one of those decisions:
the registry holds ONE slot, anything can take it, and what a drain said
about the unmapped queue has to outlive losing it. Three windows, all real:

  1. the slot is claimed one statement after the loop's own drain starts, so
     a loop that rereads the registry there is holding somebody else's job;
  2. a library refresh lands after a drain that CLEARED its signature, while
     the loop is still settling its own drain; and
  3. the same, once that settlement is already done, so the clear has to
     arrive through the adoption path instead.

Nothing here supplies state. A trace hook decides WHEN a real deletion or a
real POST lands; every job, signature and verdict is produced by the
production route, worker and CLI.

Regression lines:
  - if a drain that loses the slot one statement after starting is unbookable
    then broken
  - if a library refresh taking the slot hides a newer drain's cleared
    signature then broken
  - if it hides that clear from the adoption path as well then broken
"""
from __future__ import annotations

import sys
import time
from contextlib import contextmanager

import pytest

from apps.analysis import backlog as backlog_mod
from apps.webui.server.routes import ingest as ingest_mod
from tests.webui.analysis.conftest import (
    _audio,
    _enable_all_steps,
    _evicting_one_target_at_the_handoff,
    _seed,
    _wait,
    _watcher,
)


@contextmanager
def _a_library_refresh_taking_the_slot_as_the_drain_starts(client):
    """Run a real library refresh at the return of the locked start.

    That is the window this case is about, and it is one statement wide: the
    reconcile loop starts its drain and then has to hold on to THAT job. A
    short drain can finish and a second request claim the one slot before the
    loop's next line, so a loop that reread the registry there would be
    holding the newcomer.

    Staged rather than raced, because a one-statement window is not something
    a test can hit reliably. Everything in it is production: the hook waits
    for the drain the loop just started to really finish, then drives the
    production ``POST /ingest/refresh`` to claim the slot with a real
    library-scope job run by the real worker. The tracer is restored before
    any of that, so only the handoff itself is traced.

    Yields the list the drain job lands in, so the caller can assert against
    the signature the worker actually published.
    """
    drains: list[object] = []
    starting = ingest_mod._start_refresh_job.__code__
    previous = sys.gettrace()

    def at_the_locked_start(frame, event, arg):
        if frame.f_code is starting:
            return when_it_returns
        return None

    def when_it_returns(frame, event, arg):
        if event != "return" or arg is None:
            return when_it_returns
        sys.settrace(previous)
        drains.append(arg)
        _wait_for_job(arg)
        assert client.post(
            "/api/v1/ingest/refresh", json={"scope": "library"}
        ).status_code == 202
        _wait(client)
        return None

    sys.settrace(at_the_locked_start)
    try:
        yield drains
    finally:
        sys.settrace(previous)


def _wait_for_job(job, timeout_s: float = 60.0) -> None:
    """Block until ``job`` leaves the phases in which it owns the slot.

    Waiting on ``/refresh/status`` would not do here: the point of the case is
    that the slot stops describing this job, so the status route answers about
    somebody else the moment it does.
    """
    deadline = time.monotonic() + timeout_s
    while job.phase in ingest_mod.ACTIVE_PHASES:
        assert time.monotonic() < deadline, f"drain never finished: {job.phase}"
        time.sleep(0.02)


@pytest.mark.requirement("PARITY-06")
def test_the_drain_a_tick_started_survives_losing_the_slot_immediately(
    app, client, tmp_path
):
    """The loop must hold its own job, not whatever the registry holds later.

    ``consumed`` is the reader that books the queue a drain actually
    attempted, and it falls back to the drain's own signature whenever the
    slot is holding something else. That fallback is only worth anything if
    the job it names is really the one this tick started. Bind it by rereading
    ``_JOBS.current`` after the start returns and a library refresh landing in
    that one-statement window replaces it - and a library job publishes no
    queue signature, so the loop books None over the queue that just failed
    and relaunches it on the very next tick.

    The drain here fails on a file that is genuinely not audio, so the
    signature it publishes is real and is worth suppressing.
    """
    _seed(app, "slot0001", _audio(tmp_path, "slot.mp3"))
    _enable_all_steps(client)
    watcher = _watcher(app)

    with _a_library_refresh_taking_the_slot_as_the_drain_starts(client) as drains:
        assert watcher.tick() == "started"

    assert len(drains) == 1, (
        "fixture precondition: the locked start was never observed returning"
    )
    ours = drains[0]
    assert ours.queue_signature, (
        "fixture precondition: the drain has to publish a signature worth "
        "booking, or this case cannot tell the two jobs apart"
    )
    intruder = ingest_mod._JOBS.current
    assert intruder is not ours and intruder.scope != ingest_mod.UNMAPPED_SCOPE, (
        "fixture precondition: a DIFFERENT job must hold the slot by now"
    )
    assert intruder.queue_signature is None, (
        "fixture precondition: a library job publishes no queue signature"
    )

    assert watcher.tick() == "unchanged", (
        "the loop booked the intruder's empty signature over the queue its "
        "own drain just failed on, so that identical queue relaunches now"
    )
    assert app.state.auto_analyze.last_signature == ours.queue_signature


@pytest.mark.requirement("PARITY-06")
def test_a_cleared_signature_outlives_a_library_refresh_taking_the_slot(
    app, client, tmp_path
):
    """The one slot is replaceable; what a drain said about the queue is not.

    Same statement as the case above - a manual drain publishes None because
    the CLI reported a target it never admitted, so its queue must be retried
    rather than booked - but read one job later. A library or batch refresh
    landing after that drain replaces the slot, and a reader that asks the
    slot then sees only a job that was never about this backlog. Treating
    that as a silence and falling back to the watcher's own older signature
    reinstates the booking the clearing existed to prevent, and the lost
    target sits behind `unchanged` for as long as it keeps its content token.

    So the newest FINISHED unmapped drain is kept beside the slot
    (`_JOBS.last_unmapped`) and read from there. Real throughout: two
    production drains, a production library refresh run by the production
    worker, and a real unlink at the real moment.
    """
    _enable_all_steps(client)
    watcher = _watcher(app)
    doomed = _audio(tmp_path, "evicted.mp3")
    _seed(app, "gone0002", doomed)
    _seed(app, "stay0002", _audio(tmp_path, "stays.mp3"))

    assert watcher.tick() == "started"
    _wait(client)
    ours = ingest_mod._JOBS.current.queue_signature
    assert ours, "fixture precondition: the watcher's own drain published a queue"

    with _evicting_one_target_at_the_handoff(doomed):
        assert client.post("/api/v1/analysis-queue/run").status_code == 202
        status = _wait(client)
    assert status["phase"] == "error", status["log_tail"]
    assert ingest_mod._JOBS.current.queue_signature is None, (
        "fixture precondition: the newer drain must have CLEARED its signature"
    )

    assert client.post(
        "/api/v1/ingest/refresh", json={"scope": "library"}
    ).status_code == 202
    _wait(client)
    replacement = ingest_mod._JOBS.current
    assert replacement.scope != ingest_mod.UNMAPPED_SCOPE, (
        "fixture precondition: a job that never targeted this backlog now "
        "holds the slot, which is what hides the drain before it"
    )

    assert watcher.tick() != "unchanged"
    assert app.state.auto_analyze.last_signature != ours, (
        "a library refresh hid the cleared drain behind it, so the watcher "
        "booked its own older signature over a queue that was never attempted"
    )
    assert app.state.auto_analyze.last_signature is None, (
        "a cleared signature books nothing, so the next tick has to retry"
    )


@pytest.mark.requirement("PARITY-06")
def test_a_settled_bookings_clear_survives_a_library_refresh_taking_the_slot(
    app, client, tmp_path
):
    """The same statement, arriving through the ADOPTION path instead.

    ``consumed()`` carries a deliberate clear only while the loop is still
    settling its own drain. Once that has settled, a manual drain's clear
    reaches the ledger through the adoption of an attempt this loop did not
    start - and that path reads the one slot, so a library refresh landing
    after the drain hides it exactly as it hid it from ``consumed()``. Both
    readers therefore have to look beside the slot, or the fix reaches half
    the problem and the other half returns one job later.

    Real throughout: the watcher's own drain settles first, a production
    manual drain loses a real target, a production library refresh replaces
    the slot, and the tick after has to see the clear anyway.
    """
    _enable_all_steps(client)
    watcher = _watcher(app)
    doomed = _audio(tmp_path, "evicted.mp3")
    _seed(app, "gone0003", doomed)
    _seed(app, "stay0003", _audio(tmp_path, "stays.mp3"))

    assert watcher.tick() == "started"
    _wait(client)
    assert watcher.tick() == "unchanged", (
        "fixture precondition: the watcher's own attempt must SETTLE first, "
        "or this exercises the awaiting-drain path instead of this one"
    )
    settled = app.state.auto_analyze.last_signature
    assert settled, "fixture precondition: a signature was booked to clear"

    with _evicting_one_target_at_the_handoff(doomed):
        assert client.post("/api/v1/analysis-queue/run").status_code == 202
        status = _wait(client)
    assert status["phase"] == "error", status["log_tail"]
    assert ingest_mod._JOBS.current.queue_signature is None, (
        "fixture precondition: the newer drain must have CLEARED its signature"
    )

    assert client.post(
        "/api/v1/ingest/refresh", json={"scope": "library"}
    ).status_code == 202
    _wait(client)
    assert ingest_mod._JOBS.current.scope != ingest_mod.UNMAPPED_SCOPE, (
        "fixture precondition: a job that never targeted this backlog now "
        "holds the slot, which is what hides the drain before it"
    )

    assert watcher.tick() != "unchanged"
    assert app.state.auto_analyze.last_signature != settled, (
        "a library refresh hid the cleared drain, so a settled booking "
        "survived a drain that says its queue was never attempted"
    )
    assert app.state.auto_analyze.last_signature is None, (
        "a cleared signature books nothing, so the next tick has to retry"
    )


@contextmanager
def _a_manual_drain_running_to_completion_inside_the_scan(client):
    """Run a real manual unmapped drain at the entry to the production scan.

    That is the window this case is about, and unlike the one-statement
    handoff above it is WIDE: the scan resolves and stats every unmapped
    track, so on a network volume or a sleeping disk it is the slowest thing
    a tick does. A whole manual drain fits inside it comfortably.

    Staged at the scan's own code object, so it cannot drift onto a helper or
    a test double. The tracer restores the previous hook before it does
    anything, so the drain it starts - which scans again itself - is not
    traced, and only this one entry is.

    Yields the list the manual job lands in, so the caller can assert against
    the signature that drain actually published.
    """
    drains: list[object] = []
    scanning = backlog_mod.scan.__code__
    previous = sys.gettrace()

    def at_the_scan(frame, event, arg):
        if frame.f_code is scanning:
            sys.settrace(previous)
            assert client.post(
                "/api/v1/ingest/refresh", json={"scope": "unmapped"}
            ).status_code == 202
            _wait(client)
            drains.append(ingest_mod._JOBS.last_unmapped)
        # Falling off the end returns None, which is how CPython is told not
        # to install a local trace function - so only `call` events fire.

    sys.settrace(at_the_scan)
    try:
        yield drains
    finally:
        sys.settrace(previous)


@pytest.mark.requirement("PARITY-06")
def test_a_manual_drain_finishing_inside_the_scan_is_still_booked(
    app, client, tmp_path
):
    """The newest finished attempt has to be read AFTER the slow part.

    A tick asks who the slot is holding, walks the library, and only then
    decides. Reading the attempt ledger before that walk dates the answer by
    however long the walk took, which is exactly as long as the library is
    big. A manual drain that starts and finishes in that window is invisible:
    it failed per-track, so it left the backlog and the queue signature
    byte-for-byte where it found them, and the tick that could not see it
    relaunches the identical drain it just watched fail.

    So the ledger is read after the scan, leaving a window between the read
    and the start that is two comparisons wide - too narrow for a drain to be
    spawned, run and reaped inside.

    Real throughout: a production POST, the production worker, the production
    CLI failing on a file that is genuinely not audio, and the production
    scan deciding when it lands.
    """
    _seed(app, "race0001", _audio(tmp_path, "race.mp3"))
    _enable_all_steps(client)
    watcher = _watcher(app)

    with _a_manual_drain_running_to_completion_inside_the_scan(client) as drains:
        outcome = watcher.tick()

    assert len(drains) == 1, (
        "fixture precondition: the production scan was never observed starting"
    )
    manual = drains[0]
    assert manual is not None and manual.queue_signature, (
        "fixture precondition: the manual drain has to publish a signature, "
        "or there is nothing for the tick to have missed"
    )
    assert ingest_mod.unmapped_backlog().signature == manual.queue_signature, (
        "fixture precondition: a per-track failure must leave the queue "
        "identical, or the tick would be right to start over it"
    )

    assert outcome == "unchanged", (
        "the tick read the attempt ledger before its own scan, so a drain "
        "that failed inside that window was never booked and the identical "
        "queue was relaunched immediately"
    )
    assert app.state.auto_analyze.last_signature == manual.queue_signature


@contextmanager
def _a_manual_drain_completing_between_the_ledger_and_the_claim(client):
    """Run a real manual unmapped drain at the entry to the locked start.

    The narrowest window there is. The tick has read the attempt ledger and
    made its two comparisons; the next thing it does is claim the slot. A
    drain that starts and finishes in there is one the tick cannot have seen,
    and nothing about the gap being short makes it impossible - a descheduled
    watcher thread is descheduled for as long as the scheduler says.

    Staged at ``_start_refresh_job``'s own code object, on the CALL event, so
    it lands before the registry lock is taken and cannot deadlock against
    the manual POST it makes. The tracer is restored first, so the nested
    start that POST performs is not traced.

    Yields the list the manual job lands in.
    """
    drains: list[object] = []
    starting = ingest_mod._start_refresh_job.__code__
    previous = sys.gettrace()

    def at_the_claim(frame, event, arg):
        if frame.f_code is starting:
            sys.settrace(previous)
            assert client.post(
                "/api/v1/ingest/refresh", json={"scope": "unmapped"}
            ).status_code == 202
            _wait(client)
            drains.append(ingest_mod._JOBS.last_unmapped)
        # Falling off the end returns None, which is how CPython is told not
        # to install a local trace function - so only `call` events fire.

    sys.settrace(at_the_claim)
    try:
        yield drains
    finally:
        sys.settrace(previous)


@pytest.mark.requirement("PARITY-06")
def test_a_drain_finishing_at_the_slot_claim_is_booked_not_repeated(
    app, client, tmp_path
):
    """Reading the ledger and claiming the slot has to be ONE step.

    Moving the ledger read behind the scan narrowed the window to two
    comparisons; it did not close it. Two comparisons is a duration, and a
    thread the scheduler has parked can be parked across it, so a manual
    drain can still start, run and be reaped between what the tick knows and
    what it does about it. The queue it failed on is unchanged, so the tick
    starts a second drain over it - the retry-suppression contract broken by
    the same shape as before, one layer down.

    So the tick hands its refusal to the start as a guard, and the guard runs
    inside the registry's own lock: the ledger check and the slot claim
    cannot be separated because there is no longer a moment between them.

    Real throughout: a production POST, the production worker, the production
    CLI failing on a file that is genuinely not audio.
    """
    _seed(app, "claim001", _audio(tmp_path, "claim.mp3"))
    _enable_all_steps(client)
    watcher = _watcher(app)

    with _a_manual_drain_completing_between_the_ledger_and_the_claim(client) as drains:
        outcome = watcher.tick()

    assert len(drains) == 1, (
        "fixture precondition: the locked start was never observed being called"
    )
    manual = drains[0]
    assert manual is not None and manual.queue_signature, (
        "fixture precondition: the manual drain has to publish a signature"
    )
    assert ingest_mod.unmapped_backlog().signature == manual.queue_signature, (
        "fixture precondition: a per-track failure must leave the queue "
        "identical, or the tick would be right to start over it"
    )

    assert outcome == "unchanged", (
        "the tick claimed the slot on a ledger read it had already left "
        "behind, so it repeated the drain that finished in between"
    )
    assert ingest_mod._JOBS.last_unmapped is manual, (
        "a second drain over the identical queue was started anyway"
    )
    assert app.state.auto_analyze.last_signature == manual.queue_signature


@contextmanager
def _a_shutdown_landing_between_the_last_check_and_the_claim(watcher):
    """Call the production ``stop()`` at the entry to the locked start.

    The same window as the case above, read for a different decision. The
    tick's last look at the stop event is in front of the accounting and the
    two comparisons, so a shutdown that arrives after it - and ``stop()``
    joins with a timeout, so arriving late is the ordinary case on a library
    slow enough to scan - is invisible to everything downstream. What it
    would cost is not a wasted drain but an analyzer subprocess spawned after
    the app lifespan has already gone.

    Real shutdown: the watcher's own ``stop()``, not a poke at the event.
    """
    stopped: list[bool] = []
    starting = ingest_mod._start_refresh_job.__code__
    previous = sys.gettrace()

    def at_the_claim(frame, event, arg):
        if frame.f_code is starting:
            sys.settrace(previous)
            watcher.stop()
            stopped.append(True)
        # Falling off the end returns None, which is how CPython is told not
        # to install a local trace function - so only `call` events fire.

    sys.settrace(at_the_claim)
    try:
        yield stopped
    finally:
        sys.settrace(previous)


@pytest.mark.requirement("PARITY-06")
def test_a_shutdown_at_the_slot_claim_starts_no_drain(app, client, tmp_path):
    """A stop that lands in the claim window must still stop the drain.

    ``_decide`` checks the stop event twice - once at the top and once after
    the scan, because the scan is long enough to outlast ``stop()``'s join -
    and then does its accounting and its comparisons before claiming the
    slot. That last stretch is short, but shortness is the argument that
    already failed twice on this loop. A parked thread resuming in it starts
    a refresh worker, which spawns an analyzer subprocess, after the lifespan
    that owned it has shut down.

    So the stop check rides the same guard as the repeat check, inside the
    registry lock, and the claim cannot happen after it.
    """
    _seed(app, "stop0001", _audio(tmp_path, "stop.mp3"))
    _enable_all_steps(client)
    watcher = _watcher(app)

    with _a_shutdown_landing_between_the_last_check_and_the_claim(watcher) as stopped:
        outcome = watcher.tick()

    assert stopped == [True], (
        "fixture precondition: the locked start was never observed being called"
    )
    assert outcome == "stopping", (
        "the tick claimed the slot on a stop check it had already left behind"
    )
    assert ingest_mod._JOBS.current is None, (
        "a refresh worker was started after shutdown was requested"
    )
