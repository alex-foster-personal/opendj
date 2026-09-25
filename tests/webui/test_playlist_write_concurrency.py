"""Concurrent playlist create resilience (issue #2915).

Regression one-liners:
  - if 10 concurrent identical POST /playlists any return 500 then broken
  - if cross-connection lock contention cannot wait out busy_timeout then broken
  - if lock exhaustion returns 500 instead of 503 STATE_STORE_BUSY then broken
"""
from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server.app import create_app
from apps.webui.server.routes import playlist_write
from apps.webui.server.sqlite_backend import SqliteBackend

TRACK_IDS: list[str] = ["t-001", "t-002"]


def _fire_together(calls: list) -> list:
    barrier = threading.Barrier(len(calls))

    def _run(fn):
        barrier.wait()
        return fn()

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = [pool.submit(_run, fn) for fn in calls]
        return [f.result() for f in futures]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="test-seed")
    try:
        for i, sid in enumerate(TRACK_IDS, start=1):
            writer.upsert_track(
                stable_id=sid, stable_id_tier="inferred",
                title=f"Track {i}", artists=[f"Artist {i}"], album=None,
                isrc=None, duration_ms=180_000 + i, file_path=None,
            )
    finally:
        writer.close()
        conn.close()
    return path


@pytest.fixture
def client(db_path: Path) -> Iterator[TestClient]:
    app = create_app(
        backend=SqliteBackend(db_path), state_db_path=str(db_path),
        bind_host="127.0.0.1", hostname="test-host",
        lock_status_fn=lambda: None, mount_frontend=False,
    )
    with TestClient(app) as c:
        yield c
    playlist_write.close_store(app)


# REQ: STATE-14
def test_concurrent_create_playlist_all_201_no_500(client: TestClient) -> None:
    def _create():
        return client.post(
            "/api/v1/playlists", json={"name": "advtest-concurrency"},
        )

    responses = _fire_together([_create for _ in range(10)])
    statuses = [r.status_code for r in responses]
    assert 500 not in statuses, f"unexpected 500 responses: {[r.text for r in responses]}"
    assert all(s == 201 for s in statuses), statuses
    playlist_ids = {r.json()["playlist_id"] for r in responses}
    assert len(playlist_ids) == 10


def test_concurrent_create_under_cross_connection_contention_eventually_201(
    client: TestClient, db_path: Path,
) -> None:
    holder = state_db.open_rw(db_path, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    released = threading.Event()

    def _release_after_delay() -> None:
        time.sleep(0.2)
        holder.execute("ROLLBACK")
        holder.close()
        released.set()

    thread = threading.Thread(target=_release_after_delay)
    thread.start()

    def _create():
        return client.post(
            "/api/v1/playlists", json={"name": "contended-create"},
        )

    responses = _fire_together([_create for _ in range(5)])
    thread.join(timeout=5.0)
    released.wait(timeout=2.0)

    statuses = [r.status_code for r in responses]
    assert 500 not in statuses, f"unexpected 500 responses: {[r.text for r in responses]}"
    assert all(s == 201 for s in statuses), statuses


# REQ: STATE-14
def test_create_returns_503_when_store_busy_timeout_exhausted(
    client: TestClient, db_path: Path,
) -> None:
    warmup = client.post("/api/v1/playlists", json={"name": "warmup"})
    assert warmup.status_code == 201

    store = client.app.state.playlist_store
    store._conn.execute("PRAGMA busy_timeout = 0")

    holder = state_db.open_rw(db_path, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    try:
        response = client.post(
            "/api/v1/playlists", json={"name": "blocked-create"},
        )
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    assert response.status_code == 503, response.text
    assert response.status_code != 500
    detail = response.json()["detail"]
    assert detail["code"] == "STATE_STORE_BUSY"
    message = detail["message"].lower()
    assert "busy" in message or "locked" in message
