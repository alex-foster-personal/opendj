"""PERF-RB-04: concurrent GET /cloudsync/status share one sync-set count walk.

Regression one-liners:
  - if four status reads arrive during one walk then the walk runs once, not four times
  - if the coalescing is removed then the same harness sees four walks (mutation control)
  - if a walk finishes then the next read starts a fresh walk (nothing is cached)
  - if the shared walk fails then every caller that joined it gets the error
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.routes import cloudsync_status
from apps.webui.server.routes.cloudsync_status import SyncSetCounts
from apps.webui.server.sqlite_backend import SqliteBackend

# ----- harness -----------------------------------------------------------------

CALLERS = 4
WAIT_FOR_JOINERS_S = 10.0
COUNTS = SyncSetCounts(hash_pending=3, quarantined=2, excluded_total=7)


def _db(tmp_path: Path) -> Path:
    db_path = tmp_path / "state" / "state.db"
    state_db.open_rw(db_path, apply_schema=True).close()
    return db_path


class _SlowWalk:
    """Stands in for the real walk: holds until the other callers have joined
    (or a deadline passes) so a join can be observed without sleeps."""

    def __init__(self, db_path: Path, joiners: int, deadline_s: float) -> None:
        self.db_path, self.joiners, self.deadline_s = db_path, joiners, deadline_s
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self, db_path: Path) -> SyncSetCounts:
        with self._lock:
            self.calls += 1
        end = time.monotonic() + self.deadline_s
        while time.monotonic() < end:
            if cloudsync_status.in_flight_waiters(db_path) >= self.joiners:
                break
            time.sleep(0.01)
        return COUNTS


def _run_concurrently(fn: Callable[[], object], n: int) -> list[object]:
    out: list[object] = [None] * n

    def one(i: int) -> None:
        try:
            out[i] = fn()
        except BaseException as exc:  # collected and asserted by the caller
            out[i] = exc

    threads = [threading.Thread(target=one, args=(i,)) for i in range(n)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return out


# ----- tests -------------------------------------------------------------------


@pytest.mark.requirement("PERF-RB-04")
def test_concurrent_status_reads_share_one_walk(tmp_path: Path, monkeypatch) -> None:
    """[if] 4 status reads overlap one walk [then] it runs once, [else stop]."""
    db_path = _db(tmp_path)
    walk = _SlowWalk(db_path, joiners=CALLERS - 1, deadline_s=WAIT_FOR_JOINERS_S)
    monkeypatch.setattr(cloudsync_status, "count_sync_set", walk)
    monkeypatch.delenv("MDT_CLOUDSYNC_SCHEDULER", raising=False)
    monkeypatch.delenv("MDT_CLOUDSYNC_HUB_URL", raising=False)
    app = create_app(
        backend=SqliteBackend(db_path), bind_host="127.0.0.1",
        hostname="status-coalesce-test", state_db_path=str(db_path), mount_frontend=False,
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        responses = _run_concurrently(lambda: client.get("/api/v1/cloudsync/status"), CALLERS)

    assert walk.calls == 1, f"{walk.calls} walks ran for {CALLERS} overlapping reads"
    for response in responses:
        body = response.json()  # type: ignore[union-attr]
        assert (body["hash_pending"], body["quarantined"], body["excluded_total"]) == tuple(COUNTS)


@pytest.mark.requirement("PERF-RB-04")
def test_harness_sees_every_walk_without_coalescing(tmp_path: Path) -> None:
    """[if] reads call the walk directly [then] the harness counts 4, [else stop]."""
    db_path = _db(tmp_path)
    walk = _SlowWalk(db_path, joiners=CALLERS - 1, deadline_s=0.3)
    results = _run_concurrently(lambda: walk(db_path), CALLERS)

    assert walk.calls == CALLERS
    assert all(result == COUNTS for result in results)


@pytest.mark.requirement("PERF-RB-04")
def test_a_finished_walk_is_not_reused(tmp_path: Path, monkeypatch) -> None:
    """[if] two reads run one after another [then] two walks run, [else stop]."""
    db_path = _db(tmp_path)
    walk = _SlowWalk(db_path, joiners=0, deadline_s=0.0)
    monkeypatch.setattr(cloudsync_status, "count_sync_set", walk)

    cloudsync_status.sync_set_counts(db_path)
    cloudsync_status.sync_set_counts(db_path)

    assert walk.calls == 2
    assert cloudsync_status.in_flight_waiters(db_path) == 0


@pytest.mark.requirement("PERF-RB-04")
def test_a_failed_walk_raises_in_every_joined_caller(tmp_path: Path, monkeypatch) -> None:
    """[if] the shared walk raises [then] every joined caller raises, [else stop]."""
    db_path = _db(tmp_path)
    inner = _SlowWalk(db_path, joiners=CALLERS - 1, deadline_s=WAIT_FOR_JOINERS_S)

    def failing(path: Path) -> SyncSetCounts:
        inner(path)
        raise RuntimeError("walk failed")

    monkeypatch.setattr(cloudsync_status, "count_sync_set", failing)
    results = _run_concurrently(lambda: cloudsync_status.sync_set_counts(db_path), CALLERS)

    assert inner.calls == 1
    assert all(isinstance(result, RuntimeError) for result in results), results
    assert cloudsync_status.in_flight_waiters(db_path) == 0
