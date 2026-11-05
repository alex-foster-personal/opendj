"""Shared availability test setup and bounded worker/lock waits.

Supersedes: helper and data-directory fixture definitions in test_library_availability.py.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from apps.engine_core.library_availability import LibraryAvailabilityWorker
from apps.shared.state import db as state_db
from apps.shared.state.writer import StateWriter


def _stable(prefix: str) -> str:
    return (prefix * 40)[:40]


def _upsert_track(
    conn: sqlite3.Connection,
    stable_id: str,
    file_path: str,
    *,
    title: str | None = None,
) -> None:
    with StateWriter(conn, actor="test-availability") as writer:
        writer.upsert_track(
            stable_id=stable_id,
            stable_id_tier="inferred",
            title=title or stable_id,
            artists=[],
            album=None,
            isrc=None,
            duration_ms=None,
            file_path=file_path,
        )
    conn.commit()


def _wait_worker_complete(worker: LibraryAvailabilityWorker, *, guard_s: float) -> None:
    started = time.monotonic()
    while time.monotonic() - started < guard_s:
        snapshot = worker.status()
        if snapshot.complete and snapshot.pending == 0:
            return
        time.sleep(0.05)
    raise AssertionError(
        f"TIMEOUT: availability worker still pending={worker.status().pending} "
        f"phase={worker.status().phase} after {guard_s}s"
    )


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    target = tmp_path / "data"
    (target / "state").mkdir(parents=True)
    return target


@pytest.fixture
def state_db_path(data_dir: Path) -> Path:
    return data_dir / "state" / "state.db"


def _hold_immediate_until(state_db_path: Path, release: threading.Event) -> threading.Thread:
    """Hold a real writer lock on a second connection until ``release`` is set."""
    ready = threading.Event()

    def _hold() -> None:
        holder = state_db.open_rw(state_db_path)
        try:
            holder.execute("BEGIN IMMEDIATE")
            ready.set()
            release.wait()
            holder.execute("COMMIT")
        finally:
            holder.close()

    thread = threading.Thread(target=_hold, daemon=True)
    thread.start()
    assert ready.wait(timeout=2.0), "contending write lock never acquired"
    return thread
