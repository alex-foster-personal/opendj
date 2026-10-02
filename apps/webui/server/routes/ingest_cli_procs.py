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

import psutil

log = logging.getLogger(__name__)

#: How long stopped processes get between terminate and kill.
STOP_GRACE_S: float = 3.0

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
    """Stop every running CLI and its descendants.

    Returns how many CLIs were running, for the log line.
    """
    with _lock:
        running = list(_procs)
        _procs.clear()
    for proc in running:
        _stop_tree(proc)
    if running:
        log.info("shutdown stopped %d running pipeline CLI(s)", len(running))
    return len(running)


def _stop_tree(proc: subprocess.Popen[str]) -> None:
    """Terminate ``proc`` and everything under it, then kill what is left.

    Descendants are listed BEFORE the parent is signalled: once the parent
    dies they are reparented and no longer reachable as its children.

    The CLI itself is signalled and waited through its ``Popen``, never
    through psutil. psutil's wait would reap it behind the job thread's back,
    and ``Popen.wait`` reports a child it can no longer reap as exit 0, so a
    stopped step would read as a successful one.
    """
    try:
        descendants = psutil.Process(proc.pid).children(recursive=True)
    except psutil.NoSuchProcess:
        descendants = []
    if proc.poll() is None:
        proc.terminate()
    for member in descendants:
        with contextlib.suppress(psutil.NoSuchProcess):
            member.terminate()
    try:
        proc.wait(timeout=STOP_GRACE_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    _, alive = psutil.wait_procs(descendants, timeout=STOP_GRACE_S)
    for member in alive:
        with contextlib.suppress(psutil.NoSuchProcess):
            member.kill()
    if alive:
        psutil.wait_procs(alive, timeout=STOP_GRACE_S)


__all__ = ["STOP_GRACE_S", "register", "stop_all", "unregister"]
