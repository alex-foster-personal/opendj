"""The last coverage measurement, served at once with its age.

Measuring coverage walks the whole library: every track's path, every stem
bundle, the vocal and lyrics caches. On the preview library that is 2.8 to
5.4 s alone and 9.4 s beside a page load's other requests, and the health
lights asked for it on every poll and waited grey for the answer.

A reader that says it can accept the last measurement gets it immediately,
with ``age_s`` saying how old it is. When it is older than ``max_age_s`` one
refresh starts behind it; ``refreshing`` says so, and the reader asks again
shortly. A refresh that fails is logged and reported in ``refresh_error`` and
the next read tries again: the old value is never served as if nothing went
wrong. With no measurement yet there is nothing to serve, so the first reader
measures in its own thread and readers arriving meanwhile wait for that one
measurement rather than each starting their own.

Only ``read`` and ``peek`` serve an old value. ``measure`` always measures
(and feeds the cache), which is what a caller that acts on the number must use.

``peek`` is ``read`` for a measurement too slow to make any reader wait on,
even the first: it never measures in the caller. With no measurement yet it
starts one behind the reader and returns None, and the reader asks again.
GET /reconcile/summary needs this: its scan took 39 to 61 s on the silver
preview (Mon 5 Oct 2026) beside the ahead-analysis drain, past the browser's
30 s timeout, so a first reader that waited could never get an answer.

Requirements (mini-PRD):
  ✔︎ ✅ 🎯 HEALTH-12 the lights' first value does not wait on a re-measure
    [if] a measurement exists and a cached read arrives [then] it is returned without measuring
    [if] it is older than max_age_s [then] it is still returned, and exactly one refresh starts
    [if] four readers arrive with no measurement yet [then] one measurement runs
  ✔︎ ✅ 🎯 HEALTH-12 an old value is never passed off as current
    [if] a value is served [then] age_s is the time since it was measured
    [if] a refresh fails [then] refresh_error names it and the next read retries
    [if] the first measurement fails [then] the reader gets the error and nothing is cached
  ✔︎ HEALTH-15 a peek never waits on a measurement, not even the first
    [if] nothing is measured yet [then] peek returns None and one measurement starts behind it
    [if] four peeks arrive while it runs [then] still exactly one measurement
    [if] the first background measurement fails [then] last_refresh_error names it, the next peek retries
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: A cached read older than this starts a background refresh. Shorter than
#: the lights' 60 s refetch so every scheduled refetch does start one.
COVERAGE_MAX_AGE_S: float = 30.0

Fields = dict[str, object]
Compute = Callable[[], Fields]


@dataclass(frozen=True)
class Reading:
    fields: Fields
    #: Seconds since ``fields`` was measured.
    age_s: float
    #: A newer measurement is being taken; ask again shortly.
    refreshing: bool
    #: Why the latest background refresh failed, or None.
    refresh_error: str | None


def _spawn_daemon(job: Callable[[], None]) -> None:
    threading.Thread(target=job, name="coverage-refresh", daemon=True).start()


class CoverageCache:
    def __init__(
        self,
        *,
        max_age_s: float = COVERAGE_MAX_AGE_S,
        clock: Callable[[], float] = time.monotonic,
        spawn: Callable[[Callable[[], None]], None] = _spawn_daemon,
    ) -> None:
        self._max_age_s = max_age_s
        self._clock = clock
        self._spawn = spawn
        self._changed = threading.Condition()
        self._fields: Fields | None = None
        self._measured_at = 0.0
        self._measuring = False
        self._refresh_error: str | None = None

    #-------------------------------------------------------------------------
    def measure(self, compute: Compute) -> Reading:
        """Always measure, in the caller. The result becomes the cached value."""
        fields = compute()
        with self._changed:
            self._store(fields)
            return self._just_measured()

    def read(self, compute: Compute) -> Reading:
        """The last measurement at once; measure only when there is none."""
        with self._changed:
            while self._fields is None and self._measuring:
                self._changed.wait()
            if self._fields is not None:
                if not self._measuring and self._age() > self._max_age_s:
                    self._measuring = True
                    self._spawn(lambda: self._refresh(compute))
                return self._reading()
            self._measuring = True
        try:
            fields = compute()
        except BaseException:
            with self._changed:
                self._measuring = False
                self._changed.notify_all()
            raise
        with self._changed:
            self._measuring = False
            self._store(fields)
            self._changed.notify_all()
            return self._just_measured()

    def peek(self, compute: Compute) -> Reading | None:
        """The last measurement at once, or None while the first is taken.

        Never measures in the caller and never waits. A missing or stale
        measurement starts one refresh behind the reader (at most one runs).
        """
        with self._changed:
            if not self._measuring and (self._fields is None or self._age() > self._max_age_s):
                self._measuring = True
                start_thread = self._spawn
                start_thread(lambda: self._refresh(compute))
            return None if self._fields is None else self._reading()

    @property
    def last_refresh_error(self) -> str | None:
        """Why the latest background measurement failed, or None."""
        with self._changed:
            return self._refresh_error

    #-------------------------------------------------------------------------
    def _refresh(self, compute: Compute) -> None:
        try:
            fields = compute()
        except Exception as error:  # reported in refresh_error, and the next read retries
            logger.exception("background coverage refresh failed")
            with self._changed:
                self._measuring = False
                self._refresh_error = f"{type(error).__name__}: {error}"
                self._changed.notify_all()
            return
        with self._changed:
            self._measuring = False
            self._store(fields)
            self._changed.notify_all()

    def _store(self, fields: Fields) -> None:
        self._fields = fields
        self._measured_at = self._clock()
        self._refresh_error = None

    def _age(self) -> float:
        return max(0.0, self._clock() - self._measured_at)

    def _just_measured(self) -> Reading:
        """Measured for this very reader: age is zero by definition, not by clock."""
        assert self._fields is not None
        return Reading(self._fields, 0.0, self._measuring, None)

    def _reading(self) -> Reading:
        assert self._fields is not None
        return Reading(self._fields, self._age(), self._measuring, self._refresh_error)


__all__ = ["COVERAGE_MAX_AGE_S", "CoverageCache", "Reading"]
