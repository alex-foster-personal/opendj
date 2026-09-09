"""The machine's pressure, sampled once and shared, for stamping onto timings.

WHY THIS EXISTS. Deck-load timing rows in the browser's perf ring record what
happened and never the conditions it happened under, and
``docs/perf/performance-register.md`` is a list of numbers that cost the
program because of it: a waveform decode measured 0.73 s and 7.53 s in one
evening on nothing but the machine's load average, a beatgrid lane whose whole
runtime budget is documented as untested because it was taken at load average
554 with roughly 71 MB free. The browser cannot see any of that. This is the
one read-only door it gets.

WHAT IT REUSES. ``scripts/diagnostics/probe_native_metrics.machine_metrics``,
which is already the project's machine sampler: the launchd probe writes it
into its JSONL every 15 seconds, and it is where load average and free page
count now live too. Writing a second sampler here would guarantee the two
drift and would make the endpoint's numbers incomparable with the probe log's.

WHY IT IS CACHED. ``machine_metrics`` shells out to ``sysctl`` three times and
``vm_stat`` once. That is cheap next to walking the whole process table, which
this deliberately does NOT do, but it is not free, and the caller is a browser
polling on a timer while audio is playing. One sample is shared for
CACHE_TTL_SECONDS and every response states how old the sample it is handing
back actually is, so a caller can weigh it instead of assuming it is fresh.

WHY IT CAN REPORT NOTHING. The diagnostics package is stdlib-only and
standalone by design so the launchd probe can run on a machine with no
checkout, and the desktop payload stages ``apps`` only (see APP_SOURCE_ROOTS
in scripts/build_engine_payload.py), so ``scripts`` is absent inside a
packaged Open DJ.app. There, this reports ``available: false`` with the reason.
It never reports zeros. An unmeasured condition that renders as a real reading
is precisely the defect .claude/rules/verification.md exists to forbid.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

# One sample is worth sharing for about this long. Comfortably under the
# client's 10 s poll, so a polling client gets a fresh sample each time while
# several clients (or an agent hitting the endpoint in a loop) share one.
CACHE_TTL_SECONDS = 5.0

# The three numbers a deck-load row stamps, mapped from the sampler's keys.
# An allowlist, not a passthrough: the probe record also carries the raw swap
# string and the machine's total RAM, and a route that widened whenever the
# sampler grew a key would be a privacy decision nobody made.
_EXPOSED_FIELDS = (
    ("load_avg_1m", "load_average_1m"),
    ("mem_free_mb", "free_memory_mb"),
    ("swap_used_mb", "swap_used_mb"),
)


@dataclass(frozen=True)
class PressureSample:
    """One reading and the monotonic instant it was taken at.

    ``values`` holds only the fields the sampler could actually read. A field
    the sampler could not read is ABSENT, never present-and-zero.
    """

    taken_at: float
    values: dict[str, float]
    unavailable_reason: str | None


def _sample_machine_metrics() -> dict[str, Any]:
    """Read the project's machine sampler, importing it on first use.

    Imported inside the call rather than at module scope on purpose: this
    module is imported by the route table at engine start, and in a packaged
    app that import would fail there and take the whole route module with it.
    Deferring it turns a missing package into one endpoint that honestly says
    it cannot measure.
    """

    from scripts.diagnostics.probe_native_metrics import machine_metrics

    return machine_metrics()


def _build_sample(
    now: float, sampler: Callable[[], dict[str, Any]] = _sample_machine_metrics
) -> PressureSample:
    try:
        raw = sampler()
    except ImportError as exc:
        return PressureSample(now, {}, f"native machine sampler is not importable: {exc}")
    except OSError as exc:
        return PressureSample(now, {}, f"native machine sampler failed: {exc}")
    values = {
        exposed: float(raw[source])
        for exposed, source in _EXPOSED_FIELDS
        if isinstance(raw.get(source), (int, float))
    }
    if not values:
        return PressureSample(now, {}, "native machine sampler returned no readable field")
    return PressureSample(now, values, None)


class MachinePressureCache:
    """A single shared sample behind a TTL and a lock.

    The lock is not decoration: FastAPI runs a sync endpoint in a threadpool,
    so two polling clients land here concurrently, and without it they would
    each shell out to sysctl and vm_stat to answer the same question.
    """

    def __init__(self, ttl_seconds: float = CACHE_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._sample: PressureSample | None = None

    def read(
        self,
        *,
        now: float | None = None,
        sampler: Callable[[], dict[str, Any]] = _sample_machine_metrics,
    ) -> dict[str, Any]:
        """The current reading as a JSON-ready body, with its true age."""

        instant = time.monotonic() if now is None else now
        with self._lock:
            cached = self._sample
            if cached is None or instant - cached.taken_at >= self._ttl:
                cached = _build_sample(instant, sampler)
                self._sample = cached
        age_ms = round(max(0.0, instant - cached.taken_at) * 1000.0, 1)
        if cached.unavailable_reason is not None:
            return {
                "available": False,
                "reason": cached.unavailable_reason,
                "cache_age_ms": age_ms,
            }
        return {"available": True, "cache_age_ms": age_ms, **cached.values}


_CACHE = MachinePressureCache()


def read_machine_pressure() -> dict[str, Any]:
    """The process-wide cached reading. THE entry point for the route."""

    return _CACHE.read()
