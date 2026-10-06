"""HEALTH-15: GET /reconcile/summary?cached=true answers from the last scan.

Live on the silver preview, Mon 5 Oct 2026: the summary did not answer in
30 s (39 to 61 s per scan beside the ahead-analysis drain) while /health was
200, and every reload of the library pane started another scan.

Regression one-liners:
  - if a cached read runs the scan in the request then broken
  - if four cached reads during one scan start more than one scan then broken
  - if a cached read waits for the first scan, or reports zeros before it, then broken
  - if a stale snapshot is not served at once with refreshing true then broken
  - if a failed first scan is not named in the warming answer then broken
  - if the daemon does not start the first scan at startup then broken
  - if an uncached read stops scanning for its own request then broken
"""
from __future__ import annotations

import inspect
import logging
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import apps.webui.server.app as app_module
from apps.webui.server import coverage_cache, rb_vendor
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend, Playlist, Track
from apps.webui.server.routes import reconcile as reconcile_routes

URL = "/api/v1/reconcile/summary"
#: A cached read before any scan has finished: unknown counts, never zeros.
WARMING = {"total_tracks": None, "total_broken": None, "orphan_broken": None,
           "playlists": [], "availability": None, "computed_at": None,
           "age_s": None, "refreshing": True, "refresh_error": None}
CALLERS = 4
#: The request-time budget the browser needs (the brief's target is 100 ms
#: live); generous here so a loaded CI box does not flake it.
CACHED_READ_BUDGET_S = 0.5


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _CountingScan:
    """Stands in for the real scan, counts calls and names the calling thread."""

    def __init__(self, fail: bool = False) -> None:
        self.calls = 0
        self.threads: list[str] = []
        self.fail = fail
        self._lock = threading.Lock()

    def __call__(self, backend: object) -> dict[str, object]:
        with self._lock:
            self.calls += 1
            self.threads.append(threading.current_thread().name)
        if self.fail:
            raise RuntimeError("disk I/O error")
        return {"total_tracks": 3, "total_broken": 1, "orphan_broken": 1,
                "playlists": [], "availability": None, "computed_at": 1234.0}


@pytest.fixture
def jobs() -> list[Callable[[], None]]:
    """Background scans the cache started, held until a test runs them."""
    return []


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        jobs: list[Callable[[], None]], clock: _Clock) -> FastAPI:
    monkeypatch.setattr(rb_vendor, "bulk_rb_meta", lambda stable_ids: {})
    backend = InMemoryBackend()
    backend.seed_track(Track(stable_id="t-gone", title="Gone", artist="A",
                             file_path=str(tmp_path / "gone.mp3")))
    backend.seed_playlist(Playlist(playlist_id="pl-1", name="Warmup",
                                   vendor="djay", items=["t-gone"]))
    application = create_app(backend=backend, bind_host="127.0.0.1",
                             hostname="test-host", lock_status_fn=lambda: None,
                             mount_frontend=False)
    application.include_router(reconcile_routes.router, prefix="/api/v1")
    application.state.reconcile_summary_cache = coverage_cache.CoverageCache(
        max_age_s=reconcile_routes.SUMMARY_MAX_AGE_S, clock=clock, spawn=jobs.append,
    )
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _run_jobs(jobs: list[Callable[[], None]]) -> None:
    while jobs:
        jobs.pop(0)()


# ----- tests -------------------------------------------------------------------


@pytest.mark.requirement("HEALTH-15")
def test_cached_read_never_scans_in_the_request(
    client: TestClient, jobs: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a cached read finds no scan [then] warming 200, no scan in the request, [else stop]."""
    scan = _CountingScan()
    monkeypatch.setattr(reconcile_routes, "scan_summary", scan)

    response = client.get(URL, params={"cached": "true"})

    assert response.status_code == 200
    assert response.json() == WARMING
    assert scan.calls == 0, "the request ran the scan itself"
    assert len(jobs) == 1, "no background scan was started"


@pytest.mark.requirement("HEALTH-15")
def test_overlapping_cached_reads_start_one_scan(
    client: TestClient, jobs: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] 4 cached reads arrive before the scan ends [then] 1 scan runs, [else stop]."""
    scan = _CountingScan()
    monkeypatch.setattr(reconcile_routes, "scan_summary", scan)

    bodies = [client.get(URL, params={"cached": "true"}).json() for _ in range(CALLERS)]
    _run_jobs(jobs)
    body = client.get(URL, params={"cached": "true"}).json()

    assert bodies == [WARMING] * CALLERS
    assert scan.calls == 1
    assert (body["total_tracks"], body["total_broken"], body["refreshing"]) == (3, 1, False)


@pytest.mark.requirement("HEALTH-15")
def test_uncached_reads_without_the_cache_scan_every_time(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] reads skip the cache [then] the counter sees 4 scans, [else stop]."""
    # Mutation control for the test above: the same counter on the path that
    # does scan per request proves it can see more than one scan.
    scan = _CountingScan()
    monkeypatch.setattr(reconcile_routes, "scan_summary", scan)

    codes = [client.get(URL).status_code for _ in range(CALLERS)]

    assert codes == [200] * CALLERS
    assert scan.calls == CALLERS


@pytest.mark.requirement("HEALTH-15")
def test_a_snapshot_answers_at_once_with_its_age(
    client: TestClient, jobs: list, clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a snapshot exists [then] a cached read returns it fast with age_s, [else stop]."""
    scan = _CountingScan()
    monkeypatch.setattr(reconcile_routes, "scan_summary", scan)
    client.get(URL, params={"cached": "true"})
    _run_jobs(jobs)
    clock.now += 12.0

    started = time.perf_counter()
    response = client.get(URL, params={"cached": "true"})
    elapsed = time.perf_counter() - started

    assert response.status_code == 200
    assert response.json()["age_s"] == 12.0
    assert response.json()["refreshing"] is False
    assert elapsed < CACHED_READ_BUDGET_S
    assert scan.calls == 1 and jobs == []


@pytest.mark.requirement("HEALTH-15")
def test_a_stale_snapshot_is_served_while_one_rescan_runs(
    client: TestClient, jobs: list, clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the snapshot is past max age [then] it is served, 1 rescan starts, [else stop]."""
    scan = _CountingScan()
    monkeypatch.setattr(reconcile_routes, "scan_summary", scan)
    client.get(URL, params={"cached": "true"})
    _run_jobs(jobs)
    clock.now += reconcile_routes.SUMMARY_MAX_AGE_S + 1

    first = client.get(URL, params={"cached": "true"}).json()
    second = client.get(URL, params={"cached": "true"}).json()

    assert first["total_tracks"] == 3 and first["refreshing"] is True
    assert second["refreshing"] is True
    assert len(jobs) == 1, "a second rescan started while one was running"
    _run_jobs(jobs)
    assert client.get(URL, params={"cached": "true"}).json()["refreshing"] is False
    assert scan.calls == 2


@pytest.mark.requirement("HEALTH-15")
def test_a_failed_first_scan_is_named_and_retried(
    client: TestClient, jobs: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the first background scan fails [then] the answer names it and retries, [else stop]."""
    scan = _CountingScan(fail=True)
    monkeypatch.setattr(reconcile_routes, "scan_summary", scan)
    client.get(URL, params={"cached": "true"})
    _run_jobs(jobs)

    response = client.get(URL, params={"cached": "true"})

    assert response.status_code == 200
    assert response.json() == {**WARMING, "refresh_error": "RuntimeError: disk I/O error"}
    assert len(jobs) == 1, "the next read did not retry the scan"


@pytest.mark.requirement("HEALTH-15")
def test_the_background_scan_runs_off_the_request_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the real cache spawns the scan [then] it runs on its own thread, [else stop]."""
    monkeypatch.setattr(rb_vendor, "bulk_rb_meta", lambda stable_ids: {})
    scan = _CountingScan()
    monkeypatch.setattr(reconcile_routes, "scan_summary", scan)
    application = create_app(backend=InMemoryBackend(), bind_host="127.0.0.1",
                             hostname="test-host", lock_status_fn=lambda: None,
                             mount_frontend=False)
    application.include_router(reconcile_routes.router, prefix="/api/v1")
    with TestClient(application) as c:
        first = c.get(URL, params={"cached": "true"}).json()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            body = c.get(URL, params={"cached": "true"}).json()
            if body["computed_at"] is not None:
                break
            time.sleep(0.02)
    assert first["computed_at"] is None and first["refreshing"] is True
    assert body["total_tracks"] == 3
    assert scan.threads == ["coverage-refresh"]


@pytest.mark.requirement("HEALTH-15")
def test_the_daemon_starts_the_first_scan_at_startup(
    app: FastAPI, client: TestClient, jobs: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the daemon app is built [then] the first scan starts before any read, [else stop]."""
    scan = _CountingScan()
    monkeypatch.setattr(reconcile_routes, "scan_summary", scan)

    reconcile_routes.warm_summary_snapshot(app, app.state.backend)
    assert len(jobs) == 1 and scan.calls == 0
    assert client.get(URL, params={"cached": "true"}).json() == WARMING
    assert len(jobs) == 1, "the first read started a second scan beside the startup one"
    _run_jobs(jobs)
    assert client.get(URL, params={"cached": "true"}).json()["total_tracks"] == 3
    # The daemon factory is the one place this is wired; a refactor that
    # drops the call leaves the first read to start the scan again.
    assert "warm_summary_snapshot(app, backend)" in inspect.getsource(app_module._build_default_app)


@pytest.mark.requirement("HEALTH-15")
def test_a_slow_scan_is_logged_at_warning(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """[if] a scan reaches the slow threshold [then] it logs a WARNING, [else stop]."""
    monkeypatch.setattr(reconcile_routes, "SLOW_SCAN_WARN_S", 0.0)
    with caplog.at_level(logging.INFO, logger=reconcile_routes.__name__):
        fields = reconcile_routes.scan_summary(app.state.backend)
    slow = [r for r in caplog.records if "reconcile summary: scanned" in r.getMessage()]
    assert fields["total_tracks"] == 1 and isinstance(fields["computed_at"], float)
    assert [r.levelno for r in slow] == [logging.WARNING]
    # Control: below the threshold the same line stays at INFO.
    caplog.clear()
    monkeypatch.setattr(reconcile_routes, "SLOW_SCAN_WARN_S", 3600.0)
    with caplog.at_level(logging.INFO, logger=reconcile_routes.__name__):
        reconcile_routes.scan_summary(app.state.backend)
    assert [r.levelno for r in caplog.records if "scanned" in r.getMessage()] == [logging.INFO]
