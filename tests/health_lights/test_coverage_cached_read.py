"""The health lights get their first coverage value fast (HEALTH-12).

Regression lines:
  - if a cached read waits for a fresh measurement when an older one exists then broken
  - if a cached read hides how old its value is then broken
  - if two readers start two measurements at once then broken
  - if a failed background measurement is served as if nothing happened then broken
  - if the default read ever serves an old measurement then broken

[if] a cached coverage read waits on a new measurement while an older one exists [then] fail, [else stop].

Found on the launchd-run preview (Thu 1 Oct 2026): the coverage route
re-measured the whole library on every request, 2.8 to 5.4 s alone and 9.4 s
beside the page's other requests, so the lights sat grey well past page load
and went grey "unknown" for a further minute when a request passed 15 s.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from apps.webui.server import coverage_cache as cache_mod
from apps.webui.server.coverage_cache import CoverageCache
from tests.health_lights import fixtures as fx
from tests.health_lights.conftest import Library

pytestmark = pytest.mark.requirement("HEALTH-12")


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class _Measure:
    """A measurement whose result and failure the test controls."""

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


# ----- the cache ------------------------------------------------------------
def test_first_cached_read_measures_in_the_caller_and_is_age_zero() -> None:
    clock, jobs, measure = _Clock(), [], _Measure()
    reading = _cache(clock, jobs).read(measure)

    assert reading.fields == {"on_disk": 1}
    assert reading.age_s == 0.0
    assert reading.refreshing is False and reading.refresh_error is None
    assert measure.calls == 1 and jobs == []


def test_a_recent_value_is_served_with_its_age_and_no_new_measurement() -> None:
    clock, jobs, measure = _Clock(), [], _Measure()
    cache = _cache(clock, jobs)
    cache.read(measure)
    clock.now += 12.5

    reading = cache.read(measure)

    assert reading.age_s == 12.5
    assert reading.refreshing is False
    assert measure.calls == 1 and jobs == []


def test_an_old_value_is_served_at_once_while_one_refresh_runs_behind_it() -> None:
    clock, jobs, measure = _Clock(), [], _Measure()
    cache = _cache(clock, jobs)
    cache.read(measure)
    clock.now += 45.0
    measure.value = 2

    stale = cache.read(measure)
    again = cache.read(measure)

    # The old value, immediately: nothing measured in the caller.
    assert stale.fields == {"on_disk": 1} and stale.age_s == 45.0
    assert stale.refreshing is True and again.refreshing is True
    assert measure.calls == 1
    assert len(jobs) == 1, "a refresh already in flight must not start a second"

    jobs.pop()()
    fresh = cache.read(measure)

    assert fresh.fields == {"on_disk": 2} and fresh.age_s == 0.0
    assert fresh.refreshing is False and jobs == []


def test_a_failed_refresh_is_reported_and_retried_never_swallowed() -> None:
    clock, jobs, measure = _Clock(), [], _Measure()
    cache = _cache(clock, jobs)
    cache.read(measure)
    clock.now += 45.0
    cache.read(measure)
    measure.error = RuntimeError("state.db is locked")
    jobs.pop()()

    failed = cache.read(measure)

    assert failed.fields == {"on_disk": 1}, "the last real measurement is still what is known"
    assert failed.age_s == 45.0, "and its age keeps counting from when it was taken"
    assert failed.refresh_error == "RuntimeError: state.db is locked"
    assert failed.refreshing is True and len(jobs) == 1, "the next read tries again"

    measure.error, measure.value = None, 3
    jobs.pop()()
    healed = cache.read(measure)

    assert healed.fields == {"on_disk": 3} and healed.refresh_error is None


def test_a_first_measurement_that_fails_raises_and_caches_nothing() -> None:
    clock, jobs, measure = _Clock(), [], _Measure()
    cache = _cache(clock, jobs)
    measure.error = RuntimeError("no state.db")

    with pytest.raises(RuntimeError, match=r"no state\.db"):
        cache.read(measure)

    measure.error = None
    assert cache.read(measure).fields == {"on_disk": 1}
    assert measure.calls == 2


def test_readers_arriving_together_share_one_first_measurement() -> None:
    cache = CoverageCache(max_age_s=30.0)
    started, release = threading.Event(), threading.Event()
    calls: list[int] = []

    def slow() -> dict[str, object]:
        calls.append(1)
        started.set()
        assert release.wait(10), "the test never released the measurement"
        return {"on_disk": 7}

    results: list[dict[str, object]] = []
    readers = [
        threading.Thread(target=lambda: results.append(cache.read(slow).fields)) for _ in range(4)
    ]
    readers[0].start()
    assert started.wait(10)
    for reader in readers[1:]:
        reader.start()
    release.set()
    for reader in readers:
        reader.join(10)

    assert results == [{"on_disk": 7}] * 4
    assert len(calls) == 1, f"{len(calls)} measurements ran for readers that arrived together"


def test_measure_always_measures_and_feeds_the_cache() -> None:
    clock, jobs, measure = _Clock(), [], _Measure()
    cache = _cache(clock, jobs)
    cache.read(measure)
    measure.value = 2

    reading = cache.measure(measure)

    assert reading.fields == {"on_disk": 2} and reading.age_s == 0.0
    assert cache.read(measure).fields == {"on_disk": 2}
    assert measure.calls == 2


def test_the_default_max_age_is_shorter_than_the_lights_refetch_interval() -> None:
    """Otherwise the 60 s refetch could be answered from cache forever and
    never start a refresh."""
    assert 0 < cache_mod.COVERAGE_MAX_AGE_S < 60


# ----- the route ------------------------------------------------------------
def _add_track(library: Library, stable_id: str) -> None:
    audio: Path = fx.audio_file(library.music, f"{stable_id}.mp3")
    fx.seed_track(library.state_db, stable_id, str(audio))


def test_default_read_is_always_a_fresh_measurement(library: Library) -> None:
    """The overshoot control: caching must not reach a caller that did not
    ask for it. Scripts and the drain's callers act on this number."""
    _add_track(library, "a")
    first = library.client.get("/api/v1/ingest/coverage").json()
    _add_track(library, "b")
    second = library.client.get("/api/v1/ingest/coverage").json()

    assert (first["on_disk"], second["on_disk"]) == (1, 2)
    assert second["age_s"] == 0.0 and second["refreshing"] is False
    assert second["refresh_error"] is None


def test_cached_read_serves_the_last_value_then_the_refreshed_one(library: Library) -> None:
    jobs: list[Callable[[], None]] = []
    clock = _Clock()
    library.app.state.coverage_cache = _cache(clock, jobs)
    _add_track(library, "a")
    first = library.client.get("/api/v1/ingest/coverage?cached=true").json()
    _add_track(library, "b")
    clock.now += 45.0

    stale = library.client.get("/api/v1/ingest/coverage?cached=true").json()

    assert first["on_disk"] == 1 and first["age_s"] == 0.0
    assert stale["on_disk"] == 1, "the last computed value, not a wait for a new one"
    assert stale["age_s"] == 45.0 and stale["refreshing"] is True
    assert len(jobs) == 1

    jobs.pop()()
    fresh = library.client.get("/api/v1/ingest/coverage?cached=true").json()

    assert fresh["on_disk"] == 2 and fresh["age_s"] == 0.0 and fresh["refreshing"] is False


def test_a_fresh_default_read_updates_what_the_cached_read_serves(library: Library) -> None:
    jobs: list[Callable[[], None]] = []
    library.app.state.coverage_cache = _cache(_Clock(), jobs)
    _add_track(library, "a")
    library.client.get("/api/v1/ingest/coverage?cached=true")
    _add_track(library, "b")
    library.client.get("/api/v1/ingest/coverage")

    cached = library.client.get("/api/v1/ingest/coverage?cached=true").json()

    assert cached["on_disk"] == 2 and jobs == []
