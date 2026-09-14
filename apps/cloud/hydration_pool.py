"""Process-wide bounded parallelism for CloudSync asset hydration downloads.

CLOUDSYNC-09 part 2/2 (issue #2659): download-direction transfers acquire a
slot from this pool before hitting R2. Uploads never pass through here.

Environment overrides (fail fast on invalid values):

* ``CLOUDSYNC_HYDRATION_MAX_CONCURRENT`` - idle cap; defaults to
  :data:`DEFAULT_HYDRATION_MAX_CONCURRENT` (4) when unset.

When Part 3 pressure signals are elevated via
:func:`apps.shared.sync_runtime_gates.read_pressure_elevated` or
:func:`apps.shared.sync_runtime_gates.session_xruns_elevated`, the effective
cap is :data:`PRESSURE_HYDRATION_MAX_CONCURRENT` (1) until they clear.

``ui_mirror=None`` means session xruns are not elevated (no process-wide
mirror provider until part 5 wires one).
"""
from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from apps.shared.sync_runtime_gates import read_pressure_elevated, session_xruns_elevated

DEFAULT_HYDRATION_MAX_CONCURRENT: int = 4
PRESSURE_HYDRATION_MAX_CONCURRENT: int = 1
ENV_HYDRATION_MAX_CONCURRENT: str = "CLOUDSYNC_HYDRATION_MAX_CONCURRENT"

T = TypeVar("T")


def _configured_max_concurrent() -> int:
    raw = os.environ.get(ENV_HYDRATION_MAX_CONCURRENT)
    if raw is None or not raw.strip():
        return DEFAULT_HYDRATION_MAX_CONCURRENT
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(
            f"{ENV_HYDRATION_MAX_CONCURRENT} must be an integer; got {raw!r}"
        ) from exc


def effective_limit(
    *,
    pressure_payload: Mapping[str, Any] | None = None,
    ui_mirror: Mapping[str, Any] | None = None,
    pressure_reader: Callable[[], Mapping[str, Any]] | None = None,
) -> int:
    """Return the current hydration download concurrency cap."""
    if read_pressure_elevated(pressure_payload, reader=pressure_reader):
        return PRESSURE_HYDRATION_MAX_CONCURRENT
    if session_xruns_elevated(ui_mirror):
        return PRESSURE_HYDRATION_MAX_CONCURRENT
    return _configured_max_concurrent()


class _HydrationPool:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._in_flight = 0
        self._clock: Callable[[], float] = time.monotonic

    def in_flight(self) -> int:
        with self._condition:
            return self._in_flight

    def reset_for_tests(self, *, clock: Callable[[], float] | None = None) -> None:
        with self._condition:
            self._in_flight = 0
            if clock is not None:
                self._clock = clock
            self._condition.notify_all()

    def run_download(
        self,
        fn: Callable[[], T],
        *,
        pressure_payload: Mapping[str, Any] | None = None,
        ui_mirror: Mapping[str, Any] | None = None,
        pressure_reader: Callable[[], Mapping[str, Any]] | None = None,
    ) -> T:
        with self._condition:
            while True:
                limit = effective_limit(
                    pressure_payload=pressure_payload,
                    ui_mirror=ui_mirror,
                    pressure_reader=pressure_reader,
                )
                if self._in_flight < limit:
                    self._in_flight += 1
                    break
                self._condition.wait()
        try:
            return fn()
        finally:
            with self._condition:
                self._in_flight -= 1
                self._condition.notify_all()


_POOL = _HydrationPool()


def in_flight() -> int:
    """Return the number of download hydrations currently holding a pool slot."""
    return _POOL.in_flight()


def reset_for_tests(*, clock: Callable[[], float] | None = None) -> None:
    """Reset pool state; test-only."""
    _POOL.reset_for_tests(clock=clock)


def run_download(
    fn: Callable[[], T],
    *,
    pressure_payload: Mapping[str, Any] | None = None,
    ui_mirror: Mapping[str, Any] | None = None,
    pressure_reader: Callable[[], Mapping[str, Any]] | None = None,
) -> T:
    """Acquire a hydration download slot, run ``fn``, then release the slot."""
    return _POOL.run_download(
        fn,
        pressure_payload=pressure_payload,
        ui_mirror=ui_mirror,
        pressure_reader=pressure_reader,
    )


__all__ = [
    "DEFAULT_HYDRATION_MAX_CONCURRENT",
    "ENV_HYDRATION_MAX_CONCURRENT",
    "PRESSURE_HYDRATION_MAX_CONCURRENT",
    "effective_limit",
    "in_flight",
    "reset_for_tests",
    "run_download",
]
