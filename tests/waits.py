"""Shared waits for tests that drive a real thread, server, or OS process.

A fixed wall-clock budget polled with sleep() is a race on a shared runner,
not an assertion about the code: PR #1720 (Thu 10 Sep 2026) went red when a
correct lyric-index thread's first tick took over 15s on a loaded CI shard,
where a thread-free test of the same tick took 13s. So:

- in-process work is waited on through a SIGNAL it sets (an Event or
  Condition), with a generous hang guard that only costs time on a real hang;
- OS state with no signal (a port freeing, a process exiting) is polled via
  :func:`wait_for_external_state`, which fails as a named TIMEOUT so a slow
  runner can never read as a behavior failure of the code under test;
- a background ROUND scheduler's pacing is waited on through
  :func:`until_scheduler_idle_budget`, which charges only the time with no round
  in flight to the test's budget, because a round's own length is host fsync
  latency (nucbox-wsl-12 pushed three folder-rescan rounds past a flat 10 s,
  job 108750628416, Mon 28 Sep 2026, against 0.55 s unloaded).
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Callable, Sequence
from typing import Protocol

import uvicorn

#: Hang guard for in-process work that signals completion, NOT a performance
#: budget: the wait returns the moment the signal fires, so this only costs
#: time when the work is genuinely hung. Sized well past the slowest first
#: tick measured on a loaded CI runner (over 15s).
THREAD_HANG_GUARD_S: float = 120.0

#: Tick interval for a test that needs exactly one tick. A long interval parks
#: the thread in ``stop.wait()`` after that tick, which ``stop()`` interrupts
#: at once, so a lifespan's bounded join measures the join itself rather than
#: how fast a second tick happens to reach the disk.
PARKED_INTERVAL_S: float = 3600.0

#: Guard for OS state a test can only poll. Sized for a loaded runner.
EXTERNAL_STATE_GUARD_S: float = 60.0
EXTERNAL_STATE_POLL_S: float = 0.02
#: How often a signal wait wakes to check that its producer is still alive.
LIVENESS_CHECK_S: float = 0.5


# ----- OS state (no signal exists) --------------------------------------------


def wait_for_external_state(
    predicate: Callable[[], bool],
    *,
    what: str,
    guard_s: float = EXTERNAL_STATE_GUARD_S,
) -> float:
    """Poll until ``predicate()`` holds and return the seconds waited.

    Only for OS state that offers no signal. Raises AssertionError reading
    ``TIMEOUT: <what> ...`` so the failure names the wait, not a behavior.
    """
    started = time.monotonic()
    while not predicate():
        if time.monotonic() - started > guard_s:
            raise AssertionError(f"TIMEOUT: {what} not observed within {guard_s}s")
        time.sleep(EXTERNAL_STATE_POLL_S)
    return time.monotonic() - started


# ----- uvicorn on a thread ------------------------------------------------------


class _SignallingServer(uvicorn.Server):
    """A uvicorn Server that sets an Event the moment startup completes."""

    def __init__(self, config: uvicorn.Config) -> None:
        super().__init__(config)
        self.startup_complete = threading.Event()

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets=sockets)
        if self.started:
            self.startup_complete.set()


def start_uvicorn_in_thread(
    config: uvicorn.Config,
    *,
    what: str,
    sockets: list[socket.socket] | None = None,
) -> tuple[uvicorn.Server, threading.Thread]:
    """Run a real uvicorn server on a daemon thread; return once it serves.

    Waits on a startup Event, not a polled flag. A server thread that dies in
    startup (a bind error exits it) fails at once with its own message; only
    a live thread that never finishes startup fails as a HANG.
    """
    server = _SignallingServer(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": sockets}, daemon=True)
    thread.start()
    started = time.monotonic()
    while not server.startup_complete.wait(timeout=LIVENESS_CHECK_S):
        if not thread.is_alive():
            raise RuntimeError(f"{what}: the server thread exited during startup")
        if time.monotonic() - started > THREAD_HANG_GUARD_S:
            server.should_exit = True
            raise RuntimeError(f"HANG: {what} did not finish startup within {THREAD_HANG_GUARD_S}s")
    return server, thread


class RoundCounter(Protocol):
    """A background scheduler that counts rounds as they start and as they finish."""

    rounds_started: int
    rounds_completed: int


async def until_scheduler_idle_budget(
    schedulers: Sequence[RoundCounter],
    predicate: Callable[[], bool],
    what: str,
    *,
    idle_budget_s: float,
    round_hang_s: float = THREAD_HANG_GUARD_S,
) -> None:
    """Wait for ``predicate``, charging only scheduler idle time to ``idle_budget_s``.

    What a scheduler test asserts is PACING: that the next round starts when it
    is owed, which is measured in time with no round in flight. A round in
    flight (``rounds_started > rounds_completed`` on any of ``schedulers``) runs
    on a worker thread against a real ``state.db``, so its length is the host's
    fsync latency: it is not charged, but one unbroken stretch of it longer than
    ``round_hang_s`` fails as a hung round. A scheduler that stops starting
    rounds still fails within ``idle_budget_s``.
    """
    idle_s = 0.0
    round_s = 0.0
    last = time.monotonic()
    while not predicate():
        await asyncio.sleep(EXTERNAL_STATE_POLL_S)
        now = time.monotonic()
        step, last = now - last, now
        if any(s.rounds_started > s.rounds_completed for s in schedulers):
            round_s += step
            if round_s > round_hang_s:
                raise AssertionError(
                    f"HANG: one round ran for over {round_hang_s}s without returning "
                    f"while waiting for {what}"
                )
        else:
            round_s = 0.0
            idle_s += step
            if idle_s > idle_budget_s:
                raise AssertionError(
                    f"the scheduler spent {idle_budget_s}s with no round in flight "
                    f"while waiting for {what}"
                )
