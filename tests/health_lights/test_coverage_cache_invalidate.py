"""CoverageCache.invalidate() drops stale reconcile snapshots (issue #5862).

[if] invalidate runs after a cached reconcile snapshot [then] the next peek rescans, [else stop].
"""
from __future__ import annotations

from collections.abc import Callable

import pytest

from apps.webui.server.coverage_cache import CoverageCache

pytestmark = pytest.mark.requirement("HEALTH-5862")


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class _Measure:
    def __init__(self) -> None:
        self.calls = 0
        self.value = 1
        self.error: Exception | None = None

    def __call__(self) -> dict[str, object]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {"on_disk": self.value}


def _cache(clock: _Clock, jobs: list[Callable[[], None]], max_age_s: float = 30.0) -> CoverageCache:
    return CoverageCache(max_age_s=max_age_s, clock=clock, spawn=jobs.append)


def _drain(jobs: list[Callable[[], None]]) -> None:
    while jobs:
        jobs.pop(0)()


def test_invalidate_drops_snapshot_and_the_next_peek_rescans() -> None:
    """[if] invalidate runs after a cached value [then] peek rescans, [else stop]."""
    clock, jobs, measure = _Clock(), [], _Measure()
    cache = _cache(clock, jobs)
    cache.read(measure)
    cache.invalidate()
    measure.value = 3

    assert cache.peek(measure) is None
    assert len(jobs) == 1
    _drain(jobs)

    reading = cache.peek(measure)
    assert reading is not None
    assert reading.fields == {"on_disk": 3}


def test_cached_peek_after_drain_does_not_rescan() -> None:
    """[if] a fresh snapshot exists [then] peek does not rescan, [else stop]."""
    clock, jobs, measure = _Clock(), [], _Measure()
    cache = _cache(clock, jobs)
    cache.peek(measure)
    _drain(jobs)
    cache.peek(measure)

    before = len(jobs)
    again = cache.peek(measure)

    assert again is not None
    assert again.fields == {"on_disk": 1}
    assert len(jobs) == before


def test_invalidate_does_not_start_a_scan_by_itself() -> None:
    """[if] invalidate runs alone [then] no background job starts, [else stop]."""
    clock, jobs, measure = _Clock(), [], _Measure()
    cache = _cache(clock, jobs)
    cache.read(measure)

    cache.invalidate()

    assert jobs == []


def test_inflight_scan_before_invalidate_does_not_land() -> None:
    """[if] invalidate races an in-flight scan [then] the old result is dropped, [else stop]."""
    clock, jobs, measure = _Clock(), [], _Measure()
    cache = _cache(clock, jobs)
    cache.peek(measure)
    assert len(jobs) == 1
    measure.value = 99
    cache.invalidate()
    measure.value = 3
    _drain(jobs)

    assert cache.peek(measure) is None
    assert len(jobs) == 1
    _drain(jobs)

    reading = cache.peek(measure)
    assert reading is not None
    assert reading.fields == {"on_disk": 3}


def test_two_invalidates_still_start_one_scan_on_peek() -> None:
    """[if] invalidate runs twice [then] one peek starts one scan, [else stop]."""
    clock, jobs, measure = _Clock(), [], _Measure()
    cache = _cache(clock, jobs)
    cache.read(measure)
    cache.invalidate()
    cache.invalidate()

    assert cache.peek(measure) is None
    assert len(jobs) == 1
