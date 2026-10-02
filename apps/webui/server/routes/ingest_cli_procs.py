"""The pipeline CLIs a refresh job has running, and stopping them on shutdown.

``ingest_job._run_cli`` starts each step (``apps.analysis.run`` and friends)
as a plain child of the engine, and the analysis CLI starts its own pool of
spawned workers under it. Nothing stopped them when the engine stopped. The
engine's lifespan shutdown stops the watchers that START jobs, but a step
already running kept going, so an engine that exited on a plain SIGTERM (the
shell's parent watch, or a ``kill <pid>``) left the CLI and its pool workers
behind: three analysis workers on demon-llama, Fri 2 Oct 2026, which then
blocked the DMG installer.

So every CLI is registered here while it runs, and the lifespan calls
:func:`stop_all` on the way out. A stopped step exits on a signal, which is
not one of the per-target exit codes a refresh job continues past, so the job
ends there rather than starting its next step. The whole tree is stopped, not
just the CLI: SIGTERM kills the CLI's own process outright, and its pool
workers would then wait on a parent that no longer exists.

There is deliberately no "refuse new spawns" latch: it would be process-wide
state that outlives one app's lifespan, and the next app started in the same
process (every lifespan test) would inherit a refusal it never asked for.
"""
from __future__ import annotations

import contextlib
import logging
import subprocess
import threading
import time

import psutil

log = logging.getLogger(__name__)

#: How long stopped processes get, all together, between terminate and kill.
STOP_GRACE_S: float = 3.0
#: How long :func:`stop_all` waits for killed processes to be gone. SIGKILL
#: cannot be caught, so this only covers the kernel tearing them down.
KILL_WAIT_S: float = 1.0
#: The longest :func:`stop_all` can take, however many CLIs are running and
#: however they treat SIGTERM. The shell's grace is budgeted against this.
STOP_ALL_MAX_S: float = STOP_GRACE_S + KILL_WAIT_S

_lock = threading.Lock()
_procs: set[subprocess.Popen[str]] = set()


def register(proc: subprocess.Popen[str]) -> None:
    """Track a running CLI until :func:`unregister` or :func:`stop_all`."""
    with _lock:
        _procs.add(proc)


def unregister(proc: subprocess.Popen[str]) -> None:
    with _lock:
        _procs.discard(proc)


def stop_all() -> int:
    """Stop every running CLI and its descendants within :data:`STOP_ALL_MAX_S`.

    Every process is signalled first and then all of them share ONE grace
    window, so the total is bounded no matter how many CLIs run or how many
    of them ignore SIGTERM. Waiting tree by tree, with a grace per wait,
    added up past the shell's own grace, and its SIGKILL then landed
    mid-reap with the pool workers still running.

    Returns how many CLIs were running, for the log line.
    """
    with _lock:
        running = list(_procs)
        _procs.clear()
    if not running:
        return 0
    descendants = [member for proc in running for member in _descendants(proc)]
    _signal(running, descendants, kill=False)
    alive = _wait(running, descendants, STOP_GRACE_S)
    _signal(running, alive, kill=True)
    _wait(running, alive, KILL_WAIT_S)
    log.info("shutdown stopped %d running pipeline CLI(s)", len(running))
    return len(running)


def _signal(
    running: list[subprocess.Popen[str]], members: list[psutil.Process], *, kill: bool
) -> None:
    """Terminate (or kill) every CLI still running and every listed descendant."""
    for proc in running:
        if proc.poll() is not None:
            continue
        if kill:
            proc.kill()
        else:
            proc.terminate()
    for member in members:
        with contextlib.suppress(psutil.NoSuchProcess):
            if kill:
                member.kill()
            else:
                member.terminate()


def _wait(
    running: list[subprocess.Popen[str]], members: list[psutil.Process], within_s: float
) -> list[psutil.Process]:
    """Wait up to ``within_s`` IN TOTAL for all of them; return the members still alive."""
    deadline = time.monotonic() + within_s
    for proc in running:
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=max(0.0, deadline - time.monotonic()))
    if not members:
        return []
    _, alive = psutil.wait_procs(members, timeout=max(0.0, deadline - time.monotonic()))
    return list(alive)


def _descendants(proc: subprocess.Popen[str]) -> list[psutil.Process]:
    """Everything under ``proc``, listed BEFORE anything is signalled.

    Once the CLI dies its workers are reparented and no longer reachable as
    its children.

    The CLI itself is signalled and waited through its ``Popen``, never
    through psutil. psutil's wait would reap it behind the job thread's back,
    and ``Popen.wait`` reports a child it can no longer reap as exit 0, so a
    stopped step would read as a successful one.
    """
    try:
        return psutil.Process(proc.pid).children(recursive=True)
    except psutil.NoSuchProcess:
        return []


__all__ = ["KILL_WAIT_S", "STOP_ALL_MAX_S", "STOP_GRACE_S", "register", "stop_all", "unregister"]
