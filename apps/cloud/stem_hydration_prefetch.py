"""Next-up stem-set prefetch for AutoPlay (STEM-11747).

Split from :mod:`apps.cloud.stem_hydration` (issue #9726), which re-exports
every name here.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Literal

from apps.cloud.stem_hydration_outcomes import HydrationOutcome, _stem_source_error_from_hub_outcome
from apps.cloud.stem_source import StemSourceError

#: Logged under the facade module's name, so log filters and tests that
#: watch ``apps.cloud.stem_hydration`` see the same records after the split.
log = logging.getLogger("apps.cloud.stem_hydration")


# --- next-up prefetch (STEM-11747) ------------------------------------------------

PrefetchStatus = Literal["hydrated", "skipped", "failed"]

#: Named skip reasons, so a test and an operator grep the same token.
PREFETCH_SKIP_NOT_CLOUD = "not_cloud"
PREFETCH_SKIP_BELOW_CACHE_FLOOR = "below_cache_floor"
PREFETCH_FAILED_CODE = "STEM_PREFETCH_FAILED"


@dataclass(frozen=True)
class PrefetchOutcome:
    stable_id: str
    status: PrefetchStatus
    reason: str | None = None
    code: str | None = None


@dataclass(frozen=True)
class PrefetchDeps:
    """What one prefetch needs, bound by the caller (the route binds them to its request)."""

    state_of: Callable[[str], str]
    floor_refusal: Callable[[], str | None]
    hydrate: Callable[[str], HydrationOutcome]


def _prefetch_outcome_of(stable_id: str, outcome: HydrationOutcome) -> PrefetchOutcome:
    if outcome.status in ("hydrated", "already_local"):
        return PrefetchOutcome(stable_id, "hydrated")
    if outcome.status == "hub_error":
        hub_exc = _stem_source_error_from_hub_outcome(outcome)
        return PrefetchOutcome(stable_id, "failed", reason=hub_exc.message, code=hub_exc.code)
    return PrefetchOutcome(
        stable_id, "failed", reason=outcome.reason or outcome.status, code=outcome.code or PREFETCH_FAILED_CODE
    )


def prefetch_one(stable_id: str, deps: PrefetchDeps) -> PrefetchOutcome:
    """Prefetch one next-up stem-set from R2, or name why not. Never raises."""
    try:
        state = deps.state_of(stable_id)
        if state != "cloud":
            return PrefetchOutcome(stable_id, "skipped", reason=f"{PREFETCH_SKIP_NOT_CLOUD}: state={state}")
        refusal = deps.floor_refusal()
        if refusal is not None:
            return PrefetchOutcome(stable_id, "skipped", reason=f"{PREFETCH_SKIP_BELOW_CACHE_FLOOR}: {refusal}")
        return _prefetch_outcome_of(stable_id, deps.hydrate(stable_id))
    except StemSourceError as exc:
        return PrefetchOutcome(stable_id, "failed", reason=exc.message, code=exc.code)
    except Exception as exc:
        log.exception("stem-prefetch: unexpected failure stable_id=%s", stable_id)
        return PrefetchOutcome(stable_id, "failed", reason=f"{type(exc).__name__}: {exc}", code=PREFETCH_FAILED_CODE)


class NextUpStemPrefetcher:
    """One next-up stem-set prefetch in flight; the newest different head waits behind it.

    A changed head cancels nothing in flight. A head whose prefetch failed is not
    retried by the prefetcher (a deck load still can), and each failure code is
    logged once at WARNING until a prefetch succeeds, so a hub outage across many
    heads is one log line, never a retry storm.
    """

    def __init__(self, *, submit: Callable[[Callable[[], None]], object] | None = None) -> None:
        if submit is None:
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stem-prefetch")
            submit = executor.submit
        self._submit = submit
        self._lock = threading.Lock()
        self._in_flight: str | None = None
        self._pending: tuple[str, PrefetchDeps] | None = None
        self._failed: set[str] = set()
        self._logged_codes: set[str] = set()
        self.history: deque[PrefetchOutcome] = deque(maxlen=32)

    def offer(self, stable_id: str, deps: PrefetchDeps) -> bool:
        """Queue ``stable_id`` as the next prefetch; True when accepted. Never blocks."""
        with self._lock:
            if (
                stable_id == self._in_flight
                or stable_id in self._failed
                or (self._pending is not None and self._pending[0] == stable_id)
            ):
                return False
            if self._in_flight is not None:
                self._pending = (stable_id, deps)
                return True
            self._in_flight = stable_id
        self._start(stable_id, deps)
        return True

    def _start(self, stable_id: str, deps: PrefetchDeps) -> None:
        self._submit(lambda: self._run(stable_id, deps))

    def _run(self, stable_id: str, deps: PrefetchDeps) -> None:
        try:
            self._record(prefetch_one(stable_id, deps))
        finally:
            with self._lock:
                pending, self._pending = self._pending, None
                self._in_flight = None if pending is None else pending[0]
            if pending is not None:
                self._start(*pending)

    def _record(self, outcome: PrefetchOutcome) -> None:
        self.history.append(outcome)
        if outcome.status == "hydrated":
            self._logged_codes.clear()
            log.info("stem-prefetch: next-up stem-set hydrated stable_id=%s", outcome.stable_id)
        elif outcome.status == "skipped":
            log.info("stem-prefetch: skipped [%s] stable_id=%s", outcome.reason, outcome.stable_id)
        elif outcome.status == "failed":
            code = outcome.code or PREFETCH_FAILED_CODE
            with self._lock:
                self._failed.add(outcome.stable_id)
                first = code not in self._logged_codes
                self._logged_codes.add(code)
            log.log(
                logging.WARNING if first else logging.DEBUG,
                "stem-prefetch: next-up stem-set not prefetched [%s] stable_id=%s: %s",
                code,
                outcome.stable_id,
                outcome.reason,
            )
