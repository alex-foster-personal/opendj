"""What a reconcile tick may ADOPT as an already-attempted queue.

Split out of test_autodrain.py, which owns what a tick decides. This module
owns the narrower question underneath that decision: the one-slot job registry
can hold a drain whose verdict is not in yet, and booking one of those spends
the loop's one-shot adoption on a queue nobody can name. Both windows here are
the same production TOCTOU - ``_decide`` reads ``refresh_status().running``
and then, on the very next statement, ``last_attempted_queue()`` - so a manual
``POST /analysis-queue/run`` landing between the two reads is visible to the
second one as a RUNNING job.

The two windows differ only in how far that job has got:

  1. it has not published yet (still inside the scan), so its signature is
     None; and
  2. it has published but has not finished, so its signature is real and is
     not final - the CLI can still clear it.

Neither case supplies state. A trace hook holds one thread at a production
code object until the other has reached the point under test, so the
interleaving really happens instead of being hoped for; every job, signature
and verdict is produced by the production route, worker and CLI.

Regression lines:
  - if a drain that has published no queue is booked as attempted then broken
  - if a drain that is still running is booked before its verdict then broken
  - if a target that vanished at handoff leaves the queue booked then broken
  - if an older signature replaces a newer drain's deliberate clear then broken

What the loop must survive when the one slot CHANGES HANDS lives next door in
test_autodrain_slot.py.
"""
from __future__ import annotations

import threading

import pytest
from fastapi import HTTPException

from apps.analysis import backlog as backlog_mod
from apps.webui.server.routes import ingest as ingest_mod
from tests.webui.analysis.conftest import (
    _a_library_too_big_to_scan_quickly,
    _audio,
    _enable_all_steps,
    _evicting_one_target_at_the_handoff,
    _seed,
    _wait,
    _watcher,
)

#: Slack the scan must have over the POST-to-observation handoff for the
#: unpublished window to be readable at all.
_HANDOFF_S: float = 0.01


def _ticking_the_way_the_loop_does(watcher, raised: list[BaseException]):
    """A thread that runs one tick and survives a lost slot race, as _run does.

    The reconcile loop treats a tick that ends in a 409 as ordinary - the
    manual refresh won the slot - and logs it rather than dying, so this
    mirrors that boundary instead of inventing a new one. The booking decision
    under test happens well before ``_start()`` is reached.
    """
    def run_one_tick() -> None:
        try:
            watcher.tick()
        except HTTPException as exc:
            raised.append(exc)

    return threading.Thread(target=run_one_tick)
@pytest.mark.requirement("PARITY-06")
def test_a_drain_is_booked_only_once_it_has_published_its_queue(
    app, client, tmp_path
):
    """Booking an attempt that has published nothing spends the one adoption.

    The registry holds one slot, and the watcher adopts a signature from a
    job it did not start exactly once - identity is what stops it re-adopting
    a finished job the empty branch has deliberately forgotten. The window
    the guard exists for is a real TOCTOU between two production reads of
    that slot: ``_decide`` asks ``refresh_status().running`` and then, on the
    next statement, ``last_attempted_queue()``. A manual
    ``POST /analysis-queue/run`` landing between them is visible to the
    second read as a job with ``queue_signature`` still None. Booking it
    there spends the one-shot adoption on a queue nobody can name yet; when
    that same job publishes and fails, the watcher refuses to adopt the job
    it has already booked, ``last_signature`` stays stale, and the next tick
    relaunches the identical queue that just failed.

    Every value here is production. The tick runs the production watcher, the
    job is created by the production route and run by the production worker,
    and the signature is the one the worker itself published. What the test
    supplies is ORDER, not state: a trace hook holds the ticking thread at
    the entry to the second read until the POST has claimed the slot, so the
    interleaving the guard is written for actually happens instead of being
    hoped for. Nothing is assigned to the job, the registry or the watcher.

    An earlier version built a ``_RefreshJob`` by hand and set its ``phase``
    and ``queue_signature`` directly. It would have stayed green if the
    production worker had stopped publishing in that order at all.
    """
    _enable_all_steps(client)
    watcher = _watcher(app)
    # A library whose scan is real work, so "the worker has claimed the slot
    # but has not published yet" is a window with room in it rather than a
    # few microseconds. The fixture measures what it bought and reports
    # UNAVAILABLE if this machine is too fast for the window to exist.
    _a_library_too_big_to_scan_quickly(app, tmp_path, min_window_s=_HANDOFF_S)
    assert ingest_mod._JOBS.current is None, (
        "fixture precondition: the slot is free, so the tick's first read "
        "really does see no running job"
    )

    at_the_second_read = threading.Event()
    slot_is_claimed = threading.Event()
    worker_is_scanning = threading.Event()
    reading = watcher._attempted.__code__
    scanning = backlog_mod.scan.__code__

    def order_the_two_threads(frame, event, arg):
        if frame.f_code is reading:
            # The ticking thread has passed its running-job check and is
            # about to read the slot again. Hold it until the manual refresh
            # has claimed that slot, which is the interleaving under test.
            at_the_second_read.set()
            slot_is_claimed.wait(timeout=30)
        elif frame.f_code is scanning:
            # The worker is INSIDE the production scan, so it cannot have
            # assigned queue_signature yet: _unmapped_targets publishes only
            # once this returns. Observation, not ordering.
            worker_is_scanning.set()
        # Returning None declines a local trace, so only `call` events fire.

    raised: list[BaseException] = []
    ticking = _ticking_the_way_the_loop_does(watcher, raised)
    previous = threading.gettrace()
    threading.settrace(order_the_two_threads)
    try:
        ticking.start()
        assert at_the_second_read.wait(timeout=30), (
            "the tick never reached its second read of the job slot"
        )
        assert client.post("/api/v1/analysis-queue/run").status_code == 202
        assert worker_is_scanning.wait(timeout=30), (
            "the manual drain never entered the production scan, so there "
            "was no unpublished window for the tick to read"
        )
        assert ingest_mod._JOBS.current.queue_signature is None, (
            "fixture precondition: the worker is still inside its scan, so "
            "the second read must see an unpublished job"
        )
    finally:
        slot_is_claimed.set()
        ticking.join(timeout=30)
        threading.settrace(previous)

    assert not ticking.is_alive(), "the ticking thread never came back"
    assert [exc.status_code for exc in raised] == [409], (
        "the tick was expected to lose the slot race to the manual refresh, "
        f"which is what puts it in the window under test; got {raised}"
    )
    assert app.state.auto_analyze.attempts == 0
    assert app.state.auto_analyze.last_signature is None, (
        "the watcher booked a drain that had published no queue, so the "
        "signature it publishes next is the one it will refuse to adopt"
    )

    status = _wait(client)
    assert status["phase"] == "error", status["log_tail"]
    published = ingest_mod._JOBS.current.queue_signature
    assert published, "fixture precondition: the worker published what it read"

    assert watcher.tick() == "unchanged", (
        "the watcher relaunched the identical queue a drain had just failed "
        "on, because it booked that drain before it had published one"
    )
    assert app.state.auto_analyze.last_signature == published
    assert app.state.auto_analyze.attempts == 0


@pytest.mark.requirement("PARITY-06")
def test_a_running_drain_is_not_booked_before_its_verdict(app, client, tmp_path):
    """A published signature is not a final one until the job is finished.

    ``_unmapped_targets`` publishes ``queue_signature`` EARLY - before the
    first chunk runs - because the whole point of publishing it is to record
    the queue the worker really read rather than the watcher's own snapshot.
    So the second read of the slot can see a running job with a real
    signature, and "the newest FINISHED drain" has to be checked rather than
    assumed.

    Adopting it there is not merely premature, it is wrong in the one case
    the publish protocol exists for: when the CLI reports that a target
    vanished between the scan and its own admission check, the worker CLEARS
    ``queue_signature`` precisely so this queue is not recorded as attempted.
    A watcher that booked the earlier value has already made the decision
    that clearing was supposed to prevent, and the vanished track then sits
    behind a stale booking that no later tick reconsiders.

    The eviction is real and it happens at the real moment: the trace hook
    deletes the file at the entry to the production chunk runner, which is
    after the scan admitted it and before the CLI checks it - the exact
    window ``EXIT_MISSING_TARGETS`` names. Nothing is assigned to the job,
    and the status the worker reacts to is the one the production CLI really
    returned.
    """
    _enable_all_steps(client)
    watcher = _watcher(app)
    evicted = _audio(tmp_path, "evicted.mp3")
    _seed(app, "gone0001", evicted)
    # A second track so the backlog is still non-empty once the first one is
    # gone: an empty backlog forgets last_signature on its own, which would
    # hide an adoption that did happen behind the empty branch.
    _seed(app, "stay0001", _audio(tmp_path, "stays.mp3"))

    at_the_second_read = threading.Event()
    signature_is_published = threading.Event()
    the_tick_has_read = threading.Event()
    reading = watcher._attempted.__code__
    chunking = ingest_mod._run_analysis_chunk.__code__

    def order_the_two_threads(frame, event, arg):
        if frame.f_code is reading:
            at_the_second_read.set()
            signature_is_published.wait(timeout=30)
        elif frame.f_code is chunking:
            # Past the scan, so the signature is published; before the CLI,
            # so the target can still go the way a real eviction takes it.
            evicted.unlink()
            signature_is_published.set()
            the_tick_has_read.wait(timeout=30)
        # Returning None declines a local trace, so only `call` events fire.

    raised: list[BaseException] = []
    ticking = _ticking_the_way_the_loop_does(watcher, raised)
    previous = threading.gettrace()
    threading.settrace(order_the_two_threads)
    try:
        ticking.start()
        assert at_the_second_read.wait(timeout=30), (
            "the tick never reached its second read of the job slot"
        )
        assert client.post("/api/v1/analysis-queue/run").status_code == 202
        assert signature_is_published.wait(timeout=30), (
            "the manual drain never reached its first chunk, so there was no "
            "published-but-unfinished window for the tick to read"
        )
        published = ingest_mod._JOBS.current.queue_signature
        assert published, (
            "fixture precondition: the worker publishes before it chunks, or "
            "this test is not exercising the window it is named for"
        )
        assert ingest_mod.refresh_status().running, (
            "fixture precondition: the job under the tick's read must still "
            "be running, which is what makes its signature non-final"
        )
        ticking.join(timeout=30)
    finally:
        the_tick_has_read.set()
        ticking.join(timeout=30)
        threading.settrace(previous)

    assert not ticking.is_alive(), "the ticking thread never came back"
    assert [exc.status_code for exc in raised] == [409], (
        "the tick was expected to lose the slot race to the manual refresh, "
        f"which is what puts it in the window under test; got {raised}"
    )
    assert app.state.auto_analyze.attempts == 0
    assert app.state.auto_analyze.last_signature is None, (
        "the watcher booked a running drain's signature, so the clearing "
        "that drain is about to do can no longer reach the decision"
    )

    status = _wait(client)
    assert status["phase"] == "error", status["log_tail"]
    assert ingest_mod._JOBS.current.queue_signature is None, (
        "the target vanished between the scan and the CLI's admission check, "
        "so the worker must publish nothing for that queue - otherwise the "
        "next tick suppresses the retry of a track nothing ever analyzed"
    )


@pytest.mark.requirement("PARITY-06")
def test_a_newer_drains_cleared_signature_is_not_replaced_by_an_older_one(
    app, client, tmp_path
):
    """A cleared signature is a statement about this queue, not a silence.

    The reader bound to a started drain prefers the LATEST unmapped drain,
    because a manual one may have run since and its queue is the one worth
    suppressing. Keying that preference on the signature being non-None reads a
    DELIBERATE clear as "nothing unmapped in the slot" and falls back to the
    watcher's own older signature - which reinstates exactly the booking the
    clearing existed to prevent, one job later. The newer drain's lost target
    then sits behind a queue it never attempted.

    Only the scope can tell the two silences apart: a library or batch job
    publishes no signature because it never targeted this backlog, while an
    unmapped job that publishes None is saying its queue was not attempted.

    Real throughout. The watcher's own drain is a production tick, the manual
    one is the production route, both run the production CLI, and the eviction
    is a real unlink at the real moment.
    """
    _enable_all_steps(client)
    watcher = _watcher(app)
    doomed = _audio(tmp_path, "evicted.mp3")
    _seed(app, "gone0001", doomed)
    _seed(app, "stay0001", _audio(tmp_path, "stays.mp3"))

    assert watcher.tick() == "started"
    _wait(client)
    ours = ingest_mod._JOBS.current.queue_signature
    assert ours, "fixture precondition: the watcher's own drain published a queue"

    with _evicting_one_target_at_the_handoff(doomed):
        assert client.post("/api/v1/analysis-queue/run").status_code == 202
        status = _wait(client)
    assert status["phase"] == "error", status["log_tail"]
    newer = ingest_mod._JOBS.current
    assert newer.scope == ingest_mod.UNMAPPED_SCOPE
    assert newer.queue_signature is None, (
        "fixture precondition: the newer drain must have CLEARED its "
        "signature, which is the statement this case is about"
    )

    assert watcher.tick() != "unchanged"
    assert app.state.auto_analyze.last_signature != ours, (
        "the watcher booked its own older signature over a newer drain's "
        "deliberate clear, so the target that drain lost is suppressed by a "
        "queue nothing has attempted since"
    )
    assert app.state.auto_analyze.last_signature is None, (
        "a cleared signature books nothing, so the next tick has to retry"
    )


@pytest.mark.requirement("PARITY-06")
def test_a_settled_booking_is_cleared_by_a_later_drains_lost_target(
    app, client, tmp_path
):
    """The clearing has to reach a booking that was already settled.

    ``consumed()`` carries a deliberate ``None`` only while ``_awaiting_drain``
    is true - that is, for the drain this loop started and is still settling.
    Once that has settled, a manual drain's clear arrives through the OTHER
    path, the adoption of an attempt this loop did not start, and qualifying
    that path on a published signature skips exactly the case it most needs to
    see. The older signature stays booked, and a target that returns with the
    same content token sits behind ``unchanged`` for good even though the drain
    that lost it never admitted it.

    Scope is the only thing that separates the two silences, so the attempt
    carries it: a library or batch sweep publishes nothing because it never
    targeted this backlog, and must not be able to retire it.

    Real throughout - two production drains, the production CLI, and an unlink
    at the real handoff window.
    """
    _enable_all_steps(client)
    watcher = _watcher(app)
    doomed = _audio(tmp_path, "evicted.mp3")
    _seed(app, "gone0001", doomed)
    _seed(app, "stay0001", _audio(tmp_path, "stays.mp3"))

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

    watcher.tick()
    assert app.state.auto_analyze.last_signature is None, (
        f"a finished drain said it never admitted its queue and the booking "
        f"stayed at {settled!r}, so the target it lost is suppressed by an "
        "attempt that predates the loss"
    )

pytestmark = pytest.mark.rb_parity
