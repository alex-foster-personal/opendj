"""Bounded phases for the ahead-of-time analysis drain (NATIVE-21).

Every step of a drain tick runs here, on a worker thread, against a named
budget. A step that outlives its budget is ABANDONED, not waited on: the tick
gets a :class:`PhaseTimeout` naming the phase and the item it was on, the
log gets one warning line naming both, and the drain moves on. The abandoned
step keeps its worker until it returns by itself (a Python thread cannot be
killed), so the same phase is never started twice: until it returns, asking
for it raises :class:`PhaseStillRunning` instead of piling up a second copy.

Found live on silver, Mon 5 Oct 2026: one tick sat in SQLite for more than
11 minutes under load 60 to 80 with a 1.2 GB WAL, and nothing said which step
it was in.

Requirements (mini-PRD):
  ✔︎ a stuck step never wedges the drain (NATIVE-21)
    [if] a phase outlives its budget [then] the caller gets PhaseTimeout and a named log line
    [if] the abandoned phase is asked for again [then] PhaseStillRunning, never a second copy
    [if] the abandoned phase returns [then] the phase can run again
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

#: One worker per phase name can be held by an abandoned step at once.
PHASE_WORKERS: int = 8


class PhaseTimeout(Exception):
    """A phase outlived its budget and was abandoned."""

    def __init__(self, phase: str, budget_s: float, item: Any = None) -> None:
        self.phase = phase
        self.budget_s = budget_s
        self.item = item
        on = f" on {item}" if item else ""
        super().__init__(f"phase {phase} exceeded its {budget_s:g} s budget{on}")


class PhaseStillRunning(Exception):
    """An earlier, abandoned run of this phase has not returned yet."""

    def __init__(self, phase: str) -> None:
        self.phase = phase
        super().__init__(f"phase {phase} is still running from an earlier tick")


class PhaseRunner:
    def __init__(self, budgets_s: Mapping[str, float]) -> None:
        self._budgets = dict(budgets_s)
        self._pool = ThreadPoolExecutor(max_workers=PHASE_WORKERS, thread_name_prefix="ahead-phase")
        self._abandoned: dict[str, Future[Any]] = {}
        self._lock = threading.Lock()
        self._current: dict[str, Any] = {}
        self.timeouts: dict[str, int] = {}

    def run(self, phase: str, fn: Callable[..., T], *args: Any) -> T:
        budget = self._budgets[phase]
        with self._lock:
            earlier = self._abandoned.get(phase)
            if earlier is not None and not earlier.done():
                raise PhaseStillRunning(phase)
            if earlier is not None:
                log.info("ahead analysis: abandoned phase %s has returned; it can run again", phase)
                del self._abandoned[phase]
            self._current[phase] = None
            future = self._pool.submit(fn, *args)
        try:
            return future.result(timeout=budget)
        except FutureTimeout:
            with self._lock:
                self._abandoned[phase] = future
                self.timeouts[phase] = self.timeouts.get(phase, 0) + 1
            item = self._current.get(phase)
            log.warning(
                "ahead analysis: phase %s exceeded its %g s budget%s; abandoned, the drain moves on",
                phase, budget, f" on {item}" if item else "",
            )
            raise PhaseTimeout(phase, budget, item) from None

    def mark(self, phase: str, item: Any) -> None:
        """Record what ``phase`` is working on, so a timeout can name it."""
        self._current[phase] = item

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


__all__ = ["PhaseRunner", "PhaseStillRunning", "PhaseTimeout"]
