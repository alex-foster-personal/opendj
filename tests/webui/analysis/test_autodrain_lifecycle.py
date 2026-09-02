"""The reconcile loop's LIFECYCLE: what arms it, its thread, and shutdown.

Split out of test_autodrain.py, which owns what one TICK decides. This module
owns the loop around that tick: whether the daemon arms it at all, that the
lifespan runs and joins a real thread, and that a stop is honored no matter
where in a tick it lands. The split is the 600-line ceiling made useful -
these are the cases that need a running thread, and they were the only ones
in the file that did.

Same rule as its sibling: every case drives the watcher the PRODUCTION builder
returns (``app_mod.build_auto_analyze_watcher``), never a re-declaration of
its four callables.

Regression lines:
  - if create_app arms the auto-drain by default then broken
  - if the daemon lifespan does not run and join a real reconcile thread then broken
  - if a disabled watcher still starts a thread or a drain then broken
  - if a stopped watcher still starts a drain then broken
  - if a stop learned of during the scan is ignored then broken
  - if a thread that outlived the join is forgotten then broken
  - if a restart leaves no loop running once the survivor exits then broken
"""
from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from apps.webui.server import analysis_autostart
from apps.webui.server import app as app_mod
from apps.webui.server.routes import ingest as ingest_mod
from tests.webui.analysis.conftest import (
    _a_library_too_big_to_scan_quickly,
    _audio,
    _enable_all_steps,
    _seed,
    _watcher,
    _watching_for_the_production_scan,
)


def test_create_app_leaves_the_auto_drain_disarmed():
    """The loop shells out; only the real daemon entry points may arm it."""
    from apps.webui.server.app import create_app

    assert create_app().state.auto_analyze.enabled is False


def _reconcile_threads():
    return [t for t in threading.enumerate()
            if t.name == analysis_autostart.THREAD_NAME and t.is_alive()]


def test_lifespan_runs_a_real_reconcile_thread_and_joins_it(app, tmp_path):
    """A daemon that never starts the thread would silently never analyze.

    The real thread runs: after the lifespan opens, a thread named
    THREAD_NAME exists and has recorded a real tick outcome over the real
    (empty) backlog; after it closes, the thread is gone. Nothing here is
    stubbed, so a start() that stopped starting or a stop() that stopped
    joining both fail this test.
    """
    armed = app_mod.create_app(
        state_db_path=str(app.state.state_db), mount_frontend=False,
        port=18734, frontend_port=19734, auto_analyze=True,
    )
    armed.state.auto_analyze.interval_s = 0.05

    with TestClient(armed) as c:
        assert c.get("/api/v1/health").status_code == 200
        deadline = time.time() + 10
        while armed.state.auto_analyze.last_outcome is None and time.time() < deadline:
            time.sleep(0.05)
        assert armed.state.auto_analyze.last_outcome == "empty"
        assert _reconcile_threads(), "the lifespan did not start the loop"

    assert not _reconcile_threads(), "the lifespan did not join the loop"


def test_a_disabled_watcher_never_starts_a_thread_or_a_drain(app, client, tmp_path):
    """Disabled must mean inert, with a real backlog sitting right there.

    The library is seeded so the queue is genuinely non-empty: a watcher that
    ignored the switch would have something to drain, so "nothing happened"
    is a real result and not a vacuous one. The collaborators are the
    production ones from ``build_auto_analyze_watcher``, and the outcome is
    read off the real queue and the real one-slot job registry, so this
    cannot pass by agreeing with hand-written callables.
    """
    _seed(app, "junk0001", _audio(tmp_path, "junk.mp3"))
    _enable_all_steps(client)
    assert ingest_mod.unmapped_backlog().pending_total == 1, (
        "fixture precondition: an enabled watcher would have work to do here"
    )
    watcher = _watcher(app, enabled=False)

    watcher.start()
    try:
        assert not _reconcile_threads(), "a disabled watcher started the loop"
        assert watcher.tick() == "disabled"
    finally:
        watcher.stop()

    assert ingest_mod._JOBS.current is None, "a disabled watcher started a drain"
    assert app.state.auto_analyze.attempts == 0
    assert app.state.auto_analyze.last_signature is None



def test_a_stopped_watcher_starts_nothing_even_mid_tick(app, client, tmp_path):
    """Shutdown must win a race it is currently able to lose.

    ``stop()`` joins with a timeout. A scan or a residency probe over a slow
    volume can outlast it, so join returns with the tick still running and
    the decision path then reaches ``_start()`` and spawns an analysis
    subprocess after the app lifespan has already shut down.

    Driven through the real production watcher over a real non-empty backlog,
    so an unguarded decision path genuinely would start a drain here.
    """
    _seed(app, "stop0001", _audio(tmp_path, "stopping.mp3"))
    _enable_all_steps(client)
    watcher = _watcher(app)
    assert ingest_mod.unmapped_backlog().pending_total == 1, (
        "fixture precondition: an unguarded tick would have work to start"
    )

    watcher.stop()

    assert watcher.tick() == "stopping"
    assert ingest_mod._JOBS.current is None, "a stopped watcher started a drain"
    assert app.state.auto_analyze.attempts == 0


#: What stop() waits before giving up. Deliberately far below the measured
#: scan so the join is guaranteed to time out mid-scan.
_JOIN_S: float = 0.01


@pytest.mark.requirement("PARITY-06")
def test_stop_keeps_the_handle_of_a_reconcile_thread_that_outlived_the_join(
    app, client, tmp_path
):
    """A join that times out has not stopped anything, and must not pretend.

    Forgetting the handle there loses the only reference to a live loop, and
    the next ``start()`` - a lifespan restart in the same process - sees
    ``_thread is None`` and spawns a SECOND watcher thread alongside it.

    Nothing is injected into the watcher: it is the production thread,
    running the production tick, and the join times out because the scan in
    front of it really is longer than the join, over a library that really is
    that big.
    """
    _enable_all_steps(client)
    _a_library_too_big_to_scan_quickly(app, tmp_path, min_window_s=_JOIN_S)
    watcher = _watcher(app)

    with _watching_for_the_production_scan() as scan_has_begun:
        watcher.start()
        thread = watcher._thread
        assert scan_has_begun.wait(timeout=30), (
            "the reconcile tick never entered the production scan, so a stop "
            "here would land before it and leave this race unexercised"
        )
        try:
            watcher.stop(timeout=_JOIN_S)

            assert thread.is_alive(), (
                "fixture precondition: the join must time out"
            )
            assert watcher._thread is thread, (
                "a thread that outlived the join was forgotten, so the next "
                "start() would spawn a second one beside it"
            )
            before = len(_reconcile_threads())
            # join_timeout=0 is the production parameter at its floor, and it
            # is what pins THIS case: with any wait at all the survivor exits
            # during it and a replacement legitimately starts (its own case
            # below). Zero keeps the survivor alive across the call, which is
            # the state where a second loop beside it would be the bug.
            watcher.start(join_timeout=0)
            assert len(_reconcile_threads()) == before, (
                "start() spawned a second loop while the first was still alive"
            )
        finally:
            thread.join(timeout=30)
    assert not thread.is_alive(), "the loop never exited after its scan"


@pytest.mark.requirement("PARITY-06")
def test_a_scan_that_outlasts_stop_does_not_go_on_to_start_a_drain(
    app, client, tmp_path
):
    """Shutdown seen DURING the scan must still stop the tick.

    Checking the stop event only before the scan leaves the whole scan as a
    window: the event is set, ``stop()``'s join gives up, the app lifespan
    ends - and the tick that was already inside the scan comes back with a
    non-empty backlog and spawns an analysis subprocess into a process that
    has finished shutting down.
    """
    _enable_all_steps(client)
    _a_library_too_big_to_scan_quickly(app, tmp_path, min_window_s=_JOIN_S)
    watcher = _watcher(app)

    with _watching_for_the_production_scan() as scan_has_begun:
        watcher.start()
        thread = watcher._thread
        assert scan_has_begun.wait(timeout=30), (
            "the reconcile tick never entered the production scan, so this "
            "stop would land before it and never exercise the named race"
        )
        watcher.stop(timeout=_JOIN_S)
        assert thread.is_alive(), "fixture precondition: the join must time out"
        thread.join(timeout=30)

    assert app.state.auto_analyze.last_outcome == "stopping", (
        "a tick that learned of shutdown during its scan went on deciding"
    )
    assert ingest_mod._JOBS.current is None, (
        "a drain was started after the watcher had been stopped"
    )
    assert app.state.auto_analyze.attempts == 0


@pytest.mark.requirement("PARITY-06")
def test_a_restarted_lifespan_reuses_the_watcher_it_left_on_the_app(app, tmp_path):
    """Two lifespans on ONE app must not leave two reconcile loops.

    ``stop()`` retains the handle of a thread that outlived its join, but a
    lifespan that BUILDS a fresh watcher each time puts that handle on an
    object nobody keeps: the next start knows nothing about the survivor and
    starts a second loop beside it. Two loops share one job slot, so they
    would take turns losing a 409 race and the reconcile rate would silently
    depend on which won.

    Real throughout: the production ``create_app`` lifespan, opened and
    closed twice, with the real thread started and joined each time.
    """
    armed = app_mod.create_app(
        state_db_path=str(app.state.state_db), mount_frontend=False,
        port=18736, frontend_port=19736, auto_analyze=True,
    )
    armed.state.auto_analyze.interval_s = 0.05

    seen = []
    for _ in range(2):
        with TestClient(armed) as c:
            assert c.get("/api/v1/health").status_code == 200
            deadline = time.time() + 10
            while armed.state.auto_analyze.last_outcome is None and time.time() < deadline:
                time.sleep(0.05)
            assert armed.state.auto_analyze.last_outcome == "empty", (
                "the restarted lifespan never ran a tick, so the reused "
                "watcher was left holding a stop event nobody cleared"
            )
            assert len(_reconcile_threads()) == 1, (
                "a second reconcile loop was started beside the first"
            )
            seen.append(armed.state.auto_analyze_watcher)
        assert not _reconcile_threads(), "the lifespan did not join the loop"
        armed.state.auto_analyze.last_outcome = None

    assert seen[0] is seen[1], (
        "the second lifespan built a new watcher, so a thread retained by "
        "the first one's stop() would have been invisible to it"
    )


@pytest.mark.requirement("PARITY-06")
def test_a_restart_replaces_a_survivor_once_that_survivor_has_exited(
    app, client, tmp_path
):
    """Refusing outright left a live daemon with no reconcile loop at all.

    A survivor is not a wedged thread. ``stop()`` has already set the event
    and the loop exits the moment the scan in front of it returns - the join
    timed out because that scan is long, not because the loop stopped
    listening. So a restart that simply declined lost the race by a hair and
    then never looked again: the survivor exited a moment later, nothing
    replaced it, and every local import after that point went unanalyzed
    until some further lifespan restart. Silent, and indefinite.

    Real throughout: the production watcher, the production thread, and a
    join that times out because the library really is big enough to make the
    scan outlast it.
    """
    _enable_all_steps(client)
    _a_library_too_big_to_scan_quickly(app, tmp_path, min_window_s=_JOIN_S)
    watcher = _watcher(app)

    with _watching_for_the_production_scan() as scan_has_begun:
        watcher.start()
        survivor = watcher._thread
        assert scan_has_begun.wait(timeout=30), (
            "the reconcile tick never entered the production scan, so the "
            "stop below would land before it and leave no survivor"
        )
        watcher.stop(timeout=_JOIN_S)
        assert survivor.is_alive(), "fixture precondition: the join must time out"

        try:
            watcher.start()
            assert not survivor.is_alive(), (
                "start() returned while the survivor was still going, so it "
                "cannot have waited for it"
            )
            replacement = watcher._thread
            assert replacement is not None and replacement is not survivor, (
                "the restarted daemon was left with no reconcile loop, so "
                "nothing imported from here on is ever auto-analyzed"
            )
            assert replacement.is_alive(), "the replacement loop never ran"
            assert len(_reconcile_threads()) == 1, (
                "the replacement was started beside the survivor rather than "
                "after it"
            )
        finally:
            watcher.stop(timeout=30)

    assert not _reconcile_threads(), "the replacement loop was never joined"
