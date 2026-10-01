"""Wall-clock phase timing for one ``run_sync`` invocation.

Split out of :mod:`apps.sync_hub.client` so the client module stays under the
600-line ratchet. All math lives here; ``run_sync`` only wraps call sites in
``PhaseTimer.span``.
"""
from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal

from apps.sync_hub.client_result import SyncResult

SyncKind = Literal["first", "noop", "other"]


@dataclass(frozen=True)
class SyncTimings:
    hello_s: float
    push_s: float
    pull_s: float
    digest_s: float
    local_digest_s: float
    total_s: float
    kind: SyncKind


def classify(result: SyncResult, *, needs_full_offer_at_start: bool) -> SyncKind:
    """Label a completed sync for S12 first vs no-op scoring."""
    if needs_full_offer_at_start:
        return "first"
    if result.pushed == 0 and result.pulled == 0 and result.rounds == 1:
        return "noop"
    return "other"


class PhaseTimer:
    """Accumulate ``run_sync`` phase durations with ``time.perf_counter``."""

    def __init__(self) -> None:
        self._phase_s: dict[str, float] = {}
        self._started = time.perf_counter()

    @contextmanager
    def span(self, name: str) -> Iterator[None]:
        t0 = time.perf_counter()
        try:
            yield
        finally:
            elapsed = time.perf_counter() - t0
            self._phase_s[name] = self._phase_s.get(name, 0.0) + elapsed

    def finish(self, result: SyncResult, *, needs_full_offer_at_start: bool) -> SyncTimings:
        return SyncTimings(
            hello_s=self._phase_s.get("hello", 0.0),
            push_s=self._phase_s.get("push", 0.0),
            pull_s=self._phase_s.get("pull", 0.0),
            digest_s=self._phase_s.get("digest", 0.0),
            local_digest_s=self._phase_s.get("local_digest", 0.0),
            total_s=time.perf_counter() - self._started,
            kind=classify(result, needs_full_offer_at_start=needs_full_offer_at_start),
        )


@contextmanager
def optional_span(timer: PhaseTimer | None, name: str) -> Iterator[None]:
    if timer is None:
        yield
    else:
        with timer.span(name):
            yield


__all__ = ["PhaseTimer", "SyncKind", "SyncTimings", "classify", "optional_span"]
