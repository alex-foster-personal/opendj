"""STATE-18: a library read never waits on the state.db writer lock.

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

from .app_fixtures import _stub_rb_vendor
from .conftest import TEST_HOST_BASE_URL
from .test_rb_availability_budget import _configure_data_dir, _seed_library

pytestmark = pytest.mark.requirement("STATE-18")


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


@pytest.mark.requirement("STATE-18")
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


@pytest.mark.requirement("STATE-18")
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
            refresher.record(namespace, [(path, 131, 1_700_000_000_000_000_000) for path in paths])
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


@pytest.mark.requirement("STATE-18")
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


@pytest.mark.requirement("STATE-18")
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


def _library_jobs_client(db: Path, tmp_path: Path) -> TestClient:
    app = create_app()
    app.state.analysis_db_path = db
    app.state.stem_roots = (tmp_path / "stems",)
    return TestClient(app)


def _insert_standing_batches(conn: sqlite3.Connection) -> None:
    from apps.analysis import queue_store, queue_user
    from apps.analysis.time_iso import utc_now_iso

    now = utc_now_iso()
    model = '{"source":"user-lane","backend":"user","producer_version":"1"}'
    for lane, batch_id in queue_user.USER_BATCH_IDS.items():
        conn.execute(
            """
            INSERT OR IGNORE INTO analysis_queue_batch
                (batch_id, created_at, updated_at, state, workers, band,
                 memory_model, note)
            VALUES (?, ?, ?, ?, 1, 'user', ?, ?)
            """,
            (
                batch_id,
                now,
                now,
                queue_store.BATCH_QUEUED,
                model,
                f"user lane {lane}",
            ),
        )


@pytest.mark.requirement("STATE-18")
def test_library_jobs_ready_get_does_not_call_ensure_user_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] queue schema is ready [then] GET skips ensure_user_schema, [else stop]."""
    from apps.analysis import queue_user

    db = tmp_path / "state.db"
    client = _library_jobs_client(db, tmp_path)
    assert client.get("/api/v1/library-jobs", params={"lane": "stems"}).status_code == 200

    calls: list[object] = []

    def spy(_conn: sqlite3.Connection) -> None:
        calls.append(True)
        raise AssertionError("ensure_user_schema must not run on a ready schema read")

    monkeypatch.setattr(queue_user, "ensure_user_schema", spy)
    response = client.get("/api/v1/library-jobs", params={"lane": "stems"})
    assert response.status_code == 200, response.text
    assert not calls


@pytest.mark.requirement("STATE-18")
def test_library_jobs_first_get_provisions_empty_db(tmp_path: Path) -> None:
    """[if] state.db has no queue tables [then] first GET provisions batches, [else stop]."""
    from apps.analysis import queue_user

    db = tmp_path / "state.db"
    client = _library_jobs_client(db, tmp_path)
    response = client.get("/api/v1/library-jobs", params={"lane": "stems"})
    assert response.status_code == 200, response.text
    conn = sqlite3.connect(str(db))
    try:
        batches = {row[0] for row in conn.execute("SELECT batch_id FROM analysis_queue_batch")}
    finally:
        conn.close()
    assert set(queue_user.USER_BATCH_IDS.values()) <= batches


@pytest.mark.requirement("STATE-18")
def test_library_jobs_first_get_adds_missing_position_column(tmp_path: Path) -> None:
    """[if] position column is missing [then] first GET adds it, [else stop]."""
    from apps.analysis import queue_store
    from apps.analysis.queue_batch_lease import ensure_batch_lease_columns

    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    try:
        for sql in queue_store.QUEUE_TABLES_SQL:
            conn.execute(sql)
        ensure_batch_lease_columns(conn)
        queue_store.ensure_claimed_audio_hash_column(conn)
        _insert_standing_batches(conn)
        conn.commit()
    finally:
        conn.close()

    client = _library_jobs_client(db, tmp_path)
    assert client.get("/api/v1/library-jobs", params={"lane": "stems"}).status_code == 200

    conn = sqlite3.connect(str(db))
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(analysis_queue_item)")}
    finally:
        conn.close()
    assert "position" in cols


@pytest.mark.requirement("STATE-18")
def test_library_jobs_first_get_inserts_missing_standing_batch(tmp_path: Path) -> None:
    """[if] a standing batch row is missing [then] first GET inserts it, [else stop]."""
    from apps.analysis import queue_user

    db = tmp_path / "state.db"
    client = _library_jobs_client(db, tmp_path)
    assert client.get("/api/v1/library-jobs", params={"lane": "stems"}).status_code == 200

    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "DELETE FROM analysis_queue_batch WHERE batch_id = ?",
            (queue_user.USER_BATCH_IDS["genre"],),
        )
        conn.commit()
    finally:
        conn.close()

    assert client.get("/api/v1/library-jobs", params={"lane": "stems"}).status_code == 200
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT 1 FROM analysis_queue_batch WHERE batch_id = ?",
            (queue_user.USER_BATCH_IDS["genre"],),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
