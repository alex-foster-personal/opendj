"""STATE-16: a library read never waits on the state.db writer lock.

[if] a writer holds state.db [then] GET /api/v1/tracks still answers 200, [else stop].
"""
from __future__ import annotations

import functools
import logging
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.webui.server import path_availability_refresh
from apps.webui.server.app import create_app
from apps.webui.server.rb_vendor_pkg import availability, path_index
from apps.webui.server.sqlite_backend import SqliteBackend

from .conftest import TEST_HOST_BASE_URL, _stub_rb_vendor
from .test_rb_availability_budget import _configure_data_dir, _seed_library

pytestmark = pytest.mark.requirement("STATE-16")


@contextmanager
def _writer_lock_held(state_db_path: Path) -> Iterator[None]:
    """Hold the state.db writer lock from another connection, as a long sync batch does."""
    holder = sqlite3.connect(str(state_db_path), isolation_level=None, timeout=0)
    holder.execute("BEGIN IMMEDIATE")
    try:
        yield
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def _indexed_sizes(state_db_path: Path, data_dir: Path, paths: list[str]) -> dict[str, int | None]:
    conn = state_db.open_ro(state_db_path)
    try:
        index = path_index.bulk_lookup(conn, path_index.resolver_namespace(data_dir), paths)
    finally:
        conn.close()
    return {path: entry.materialised_size for path, entry in index.items() if entry is not None}


def _wait_until(predicate: Callable[[], bool], timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


@pytest.mark.requirement("STATE-16")
def test_tracks_listing_answers_while_a_writer_holds_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a writer holds state.db [then] GET /tracks is 200 inside busy_timeout, [else stop]."""
    data_dir = tmp_path / "data"
    state_db_path = _configure_data_dir(monkeypatch, data_dir)
    _stub_rb_vendor(monkeypatch)
    sids, paths = _seed_library(state_db_path, track_count=3)
    app = create_app(
        backend=SqliteBackend(state_db_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(state_db_path),
        mount_frontend=False,
    )
    with TestClient(app, base_url=TEST_HOST_BASE_URL) as client:
        with _writer_lock_held(state_db_path):
            started = time.monotonic()
            response = client.get("/api/v1/tracks?limit=50")
            elapsed = time.monotonic() - started
            assert not _indexed_sizes(state_db_path, data_dir, paths), "persisted under the lock"
        assert response.status_code == 200, response.text
        assert elapsed < state_db.DEFAULT_BUSY_TIMEOUT_S / 2, f"waited {elapsed:.2f}s on the lock"
        items = response.json()["items"]
        assert sorted(item["stable_id"] for item in items) == sorted(sids)
        # The request DID stat every row (no track_availability rows), so it
        # had answers to persist; the refresher lands them once the lock frees.
        assert all(item["file_exists"] is True for item in items)
        assert _wait_until(
            lambda: len(_indexed_sizes(state_db_path, data_dir, paths)) == len(paths), 10.0
        ), "the refresher never persisted the request's stats"


@pytest.mark.requirement("STATE-16")
def test_refresher_keeps_rows_and_survives_a_busy_writer_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """[if] the lock is busy at persist [then] it logs, survives, persists later, [else stop]."""
    data_dir = tmp_path / "data"
    state_db_path = _configure_data_dir(monkeypatch, data_dir)
    _sids, paths = _seed_library(state_db_path, track_count=2)
    monkeypatch.setattr(
        state_db, "open_rw", functools.partial(state_db.open_rw, busy_timeout_s=0.2)
    )
    refresher = path_availability_refresh.PathAvailabilityRefresher(
        data_dir=data_dir, state_db_path=state_db_path
    )
    namespace = path_index.resolver_namespace(data_dir)
    caplog.set_level(logging.WARNING, logger=path_availability_refresh.__name__)
    refresher.start()
    try:
        with _writer_lock_held(state_db_path):
            refresher.record(namespace, [(path, 131) for path in paths])
            assert _wait_until(
                lambda: "writer lock still busy" in caplog.text, 5.0
            ), "a busy persist was not logged"
            assert not _indexed_sizes(state_db_path, data_dir, paths)
        assert _wait_until(
            lambda: len(_indexed_sizes(state_db_path, data_dir, paths)) == len(paths), 10.0
        ), "rows kept across the busy lock were never persisted"
        assert refresher.running, "a busy writer lock killed the refresher thread"
    finally:
        refresher.stop()


@pytest.mark.requirement("STATE-16")
def test_probe_without_a_running_refresher_persists_inline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] no refresher runs (CLI, script) [then] the probe persists its own stats, [else stop]."""
    data_dir = tmp_path / "data"
    state_db_path = _configure_data_dir(monkeypatch, data_dir)
    _sids, paths = _seed_library(state_db_path, track_count=2)
    path_availability_refresh.stop()

    probed = availability.bulk_probe_paths(paths, trust_index=False)

    assert all(result.materialised_size == 131 for result in probed.values())
    assert _indexed_sizes(state_db_path, data_dir, paths) == {path: 131 for path in paths}


@pytest.mark.requirement("STATE-16")
def test_library_jobs_listing_answers_while_a_writer_holds_the_lock(tmp_path: Path) -> None:
    """[if] a writer holds state.db [then] GET /library-jobs is 200 inside busy_timeout, [else stop]."""
    from apps.analysis import queue_user

    db = tmp_path / "state.db"
    app = create_app()
    app.state.analysis_db_path = db
    app.state.stem_roots = (tmp_path / "stems",)
    client = TestClient(app)
    assert client.get("/api/v1/library-jobs", params={"lane": "stems"}).status_code == 200
    conn = sqlite3.connect(str(db))
    try:
        batches = {row[0] for row in conn.execute("SELECT batch_id FROM analysis_queue_batch")}
    finally:
        conn.close()
    assert set(queue_user.USER_BATCH_IDS.values()) <= batches, "the standing batches were not made"

    with _writer_lock_held(db):
        started = time.monotonic()
        response = client.get("/api/v1/library-jobs", params={"lane": "stems", "include": "settled"})
        elapsed = time.monotonic() - started

    assert response.status_code == 200, response.text
    assert elapsed < state_db.DEFAULT_BUSY_TIMEOUT_S / 2, f"waited {elapsed:.2f}s on the lock"
