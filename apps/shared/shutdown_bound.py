"""Bound the engine's quit to a few seconds, with nothing left behind (W43).

A user quit of the installed app took 13.3 s on silver (Wed 7 Oct 2026,
main 2411a8f2): the shell logged ``engine pgid ... exited 13341ms after
SIGTERM`` and then had to SIGKILL processes the exited engine left behind.
Two things held it:

- a page's ``GET /commands/next?wait_ms=20000`` long poll kept uvicorn in its
  whole graceful window, then was cancelled and answered 500 (fixed where the
  broker lives, ``apps.webui.server.routes.commands``);
- the lifespan teardown stopped a dozen background services ONE AFTER
  ANOTHER, each with its own join (5 s for most, 30 s for the availability
  worker's current batch, an unbounded await for a CloudSync or folder-rescan
  round in flight), so the waits added up.

This module holds the two pieces that bound the second part:

- :func:`run_stop_steps` stops a set of services CONCURRENTLY under one shared
  deadline and logs what each one took, so the next slow quit names its
  culprit instead of hiding inside one total.
- :func:`arm_quit_watchdog` is the backstop: armed the moment SIGTERM
  arrives, it reaps every descendant process and ends the engine at
  :data:`QUIT_DEADLINE_S`, whatever is still running. Job workers lead their
  own sessions, so only the engine itself can find them (by parentage) once
  the shell's group SIGKILL has missed them.

Every background thread these steps stop is a daemon, and SQLite in WAL mode
is crash-safe, so a step that overruns is logged and left to die with the
process rather than waited on.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

import psutil

from apps.shared.process_tree import stop_processes

log = logging.getLogger("opendj.shutdown")

#: Seconds from SIGTERM to the engine's exit, at the latest. The shell's own
#: target for a user quit is 3 s; this leaves it room to reap and sweep.
QUIT_DEADLINE_S: float = 2.5
#: Seconds the background-service stops share, once the child processes are
#: reaped. Each stop runs on its own thread, so this is a bound on the
#: slowest one, not on their sum. The engine's graceful window, plus this,
#: must finish before the watchdog starts its reap (a budget test holds it).
STOP_STEPS_DEADLINE_S: float = 0.5
#: Seconds a descendant gets between SIGTERM and SIGKILL in a sweep.
REAP_GRACE_S: float = 0.2
#: Seconds the watchdog reserves before its deadline for a reap: a SIGTERM
#: grace, a SIGKILL wait, the late-fork passes, and slack for the log line
#: and the exit itself.
REAP_BUDGET_S: float = 3 * REAP_GRACE_S + 0.1
#: Seconds between looks for late forks during a reap's late-fork window.
LATE_FORK_POLL_S: float = 0.02
#: Where :func:`begin_stop_window` keeps the shared deadline on ``app.state``.
STOP_DEADLINE_ATTR: str = "shutdown_stop_deadline"

StopStep = tuple[str, Callable[[], object]]


def begin_stop_window(state: Any, window_s: float = STOP_STEPS_DEADLINE_S) -> float:
    """Open the ONE stop window an app's nested teardowns share; return its deadline.

    Set once on ``app.state`` and kept: the engine lifespan's own stops, the
    schedulers' context exits and the legacy lifespan's stops all run inside
    it, so nesting cannot give each group a full window of its own. Every
    group is still started; one that opens after the deadline is told to stop
    and simply not waited on.
    """
    deadline = getattr(state, STOP_DEADLINE_ATTR, None)
    if deadline is None:
        deadline = time.monotonic() + window_s
        setattr(state, STOP_DEADLINE_ATTR, deadline)
    return float(deadline)


def reset_stop_window(state: Any) -> None:
    """Close any earlier window, so an app whose lifespan runs again gets a fresh one."""
    setattr(state, STOP_DEADLINE_ATTR, None)


def stop_window_deadline(state: Any) -> float | None:
    """The deadline :func:`begin_stop_window` opened on ``state``, if any."""
    deadline = getattr(state, STOP_DEADLINE_ATTR, None)
    return None if deadline is None else float(deadline)


def remaining_s(deadline: float) -> float:
    """Seconds left before ``deadline`` (monotonic), never negative."""
    return max(0.0, deadline - time.monotonic())


def run_stop_steps(
    label: str,
    steps: Sequence[StopStep],
    *,
    deadline: float | None = None,
) -> list[str]:
    """Run every ``(name, stop)`` at once; wait no later than ``deadline``.

    ``deadline`` is a shared monotonic deadline from :func:`begin_stop_window`;
    without one, the steps get :data:`STOP_STEPS_DEADLINE_S` of their own.
    Every step is started even when the deadline has passed. Returns the names
    still running at the deadline. A step that raises is logged and counts as
    finished: one broken stop must not keep the others, or the process, waiting.
    """
    started = time.monotonic()
    window = "shared" if deadline is not None else "own"
    if deadline is None:
        deadline = started + STOP_STEPS_DEADLINE_S
    took: dict[str, float] = {}

    def run(name: str, stop: Callable[[], object]) -> None:
        try:
            stop()
        except Exception:
            log.exception("[shutdown] %s: %s raised", label, name)
        took[name] = time.monotonic() - started

    threads = [
        threading.Thread(target=run, args=step, name=f"stop-{step[0]}", daemon=True)
        for step in steps
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    overran = [name for name, _ in steps if name not in took]
    timings = ", ".join(f"{name} {took[name] * 1000:.0f}ms" for name, _ in steps if name in took)
    # The window's deadline is logged as its identity: groups that share one
    # window print the same value, which is how a slow quit's log (and the
    # real-engine test) shows that nesting did not give a group its own.
    log.info(
        "[shutdown] %s: %d step(s) stopped in %.0fms (%s) [%s window %.6f]",
        label, len(took), (time.monotonic() - started) * 1000, timings or "none",
        window, deadline,
    )
    if overran:
        log.warning(
            "[shutdown] %s: still stopping after %.0fms, left to end with the process: %s",
            label, (time.monotonic() - started) * 1000, ", ".join(overran),
        )
    return overran


def reap_descendants(grace_s: float = REAP_GRACE_S) -> list[str]:
    """SIGTERM every live descendant of this process, then SIGKILL the holdouts.

    Returns a ``name(pid)`` line per process found, for the log. Parentage is
    what finds a job worker that leads its own session, which a process-group
    signal never reaches.

    One snapshot is not enough: while the teardown is wedged, a job
    supervisor or a CLI that has not been stopped yet can still fork, and a
    worker forked after the snapshot would outlive ``os._exit``. So every
    pass ends by looking again, and a process that appeared in the meantime
    is killed outright (it has had no time to need a grace). The looking
    lasts the whole late-fork window (``grace_s``) whatever an early look
    finds: an empty look says nothing about a spawn that completes a moment
    later, so stopping at the first one let a worker escape.
    """
    found: list[str] = []
    seen: set[int] = set()
    first = _live_descendants(seen)
    for child in first:
        _note(child, found)
    # The seam splits one budget between its polite and forced rungs, so it
    # gets two graces: each rung keeps the grace_s it always had.
    try:
        survivors = stop_processes(first, grace_s=2 * grace_s)
    except psutil.Error as exc:
        # The watchdog must reach os._exit whatever happens here, so a failure
        # is reported in the returned lines rather than raised past it.
        found.append(f"reap failed: {exc!r}")
    else:
        found.extend(f"SURVIVED {child.pid}" for child in survivors)
    stop_looking = time.monotonic() + grace_s
    while time.monotonic() < stop_looking:
        late = _live_descendants(seen)
        for child in late:
            _note(child, found)
            _signal(child, kill=True)
        if late:
            psutil.wait_procs(late, timeout=LATE_FORK_POLL_S)
        else:
            time.sleep(LATE_FORK_POLL_S)
    return found


def _live_descendants(seen: set[int]) -> list[psutil.Process]:
    """Descendants not handled yet; records them in ``seen``."""
    try:
        children = psutil.Process().children(recursive=True)
    except psutil.Error:
        return []
    fresh = [child for child in children if child.pid not in seen]
    seen.update(child.pid for child in fresh)
    return fresh


def _note(child: psutil.Process, found: list[str]) -> None:
    try:
        found.append(f"{child.name()}({child.pid})")
    except psutil.Error:
        found.append(f"?({child.pid})")


def _signal(child: psutil.Process, *, kill: bool) -> None:
    try:
        child.kill() if kill else child.terminate()
    except psutil.Error:
        pass


class _Quit:
    #: Set once, when the quit's signal arrives. A plain bool, not an Event:
    #: it is written inside the signal handler, where taking a lock could
    #: deadlock against the very thread the signal interrupted.
    started: bool = False


class SpawnAfterQuit(RuntimeError):
    """A worker was about to be started after the engine began to quit."""


def quit_started() -> bool:
    """True once the quit's signal has armed the watchdog."""
    return _Quit.started


#: Audit events that start a process. ``subprocess.Popen`` covers
#: ``subprocess.run`` and asyncio's subprocesses too.
_SPAWN_EVENTS = frozenset(
    {"subprocess.Popen", "os.fork", "os.forkpty", "os.posix_spawn", "os.spawn", "os.system"}
)


def _refuse_spawns_once_quitting(event: str, _args: tuple[object, ...]) -> None:
    # Runs on EVERY audit event in the process (each open, import, socket),
    # so the cheap attribute read comes first and nothing else runs until
    # the quit has begun.
    if _Quit.started and event in _SPAWN_EVENTS:
        raise SpawnAfterQuit(f"engine is quitting; refused {event}")


def install_spawn_guard() -> None:
    """Refuse every process start in this process once the quit has begun (W43).

    The watchdog's reap looks for new children for a bounded window and then
    exits, so a fork that lands after its last look outlives the engine, and
    no look can close that gap. One audit hook refuses the fork itself where
    every ``subprocess``, asyncio and ``os`` spawn passes, instead of a gate
    per launcher that the next new launcher forgets. ``multiprocessing``'s
    own spawn raises no audit event; the engine runs no pool itself, and its
    final reap still catches one. Audit hooks cannot be removed, so only the
    engine's ``serve`` installs it.
    """
    sys.addaudithook(_refuse_spawns_once_quitting)


def _begin_quit() -> None:
    _Quit.started = True


def arm_quit_watchdog(
    deadline_s: float = QUIT_DEADLINE_S,
    *,
    exit_code: int = 0,
) -> None:
    """End this process within ``deadline_s`` from now, whatever is still running.

    Called once, when SIGTERM arrives. The reap is budgeted INSIDE the
    deadline: it starts :data:`REAP_BUDGET_S` early, so a descendant that
    ignores SIGTERM still costs no more than the deadline. Then it exits
    without running the rest of the teardown: by then a step that has not
    finished is the bug, and waiting on it is what made a quit take 13 s.

    From this call on, :func:`install_spawn_guard` refuses new processes.
    """
    _begin_quit()
    def watch() -> None:
        time.sleep(max(0.0, deadline_s - REAP_BUDGET_S))
        reaped = reap_descendants()
        _log_without_waiting(
            "[shutdown] engine still running %.0fms after SIGTERM; exiting now%s",
            deadline_s * 1000,
            f" after reaping {', '.join(reaped)}" if reaped else "",
        )
        os._exit(exit_code)

    threading.Thread(target=watch, name="quit-watchdog", daemon=True).start()


#: Seconds the watchdog's last log line may take before it exits anyway.
EXIT_LOG_WAIT_S: float = 0.05


def _log_without_waiting(message: str, *args: object) -> None:
    """Log and flush on a helper thread; give up after :data:`EXIT_LOG_WAIT_S`.

    The watchdog is the bound itself, so nothing on its path may wait on a
    lock it does not own. A handler is synchronous and locked: a thread stuck
    mid-emit (a stalled disk under a file handler) holds that lock, and a
    direct ``log.error`` or ``logging.shutdown()`` would wait on it forever.
    """
    def emit() -> None:
        log.error(message, *args)
        logging.shutdown()

    writer = threading.Thread(target=emit, name="quit-watchdog-log", daemon=True)
    writer.start()
    writer.join(EXIT_LOG_WAIT_S)


__all__ = [
    "EXIT_LOG_WAIT_S",
    "LATE_FORK_POLL_S",
    "QUIT_DEADLINE_S",
    "REAP_BUDGET_S",
    "REAP_GRACE_S",
    "STOP_DEADLINE_ATTR",
    "STOP_STEPS_DEADLINE_S",
    "SpawnAfterQuit",
    "arm_quit_watchdog",
    "begin_stop_window",
    "install_spawn_guard",
    "quit_started",
    "reap_descendants",
    "remaining_s",
    "reset_stop_window",
    "run_stop_steps",
    "stop_window_deadline",
]
