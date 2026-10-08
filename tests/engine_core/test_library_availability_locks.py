"""Availability worker retry and priority recovery under real SQLite locks.

[if] a writer lock exhausts a round's retries [then] the worker resumes after release, [else stop].

Supersedes: lock-retry tests at the end of test_library_availability.py.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from apps.engine_core.library_availability import LibraryAvailabilityWorker
from apps.shared.state import db as state_db
from tests.engine_core.availability_test_support import (
    _hold_immediate_until,
    _stable,
    _upsert_track,
    _wait_worker_complete,
)
from tests.engine_core.availability_test_support import (
    data_dir as data_dir,
)
from tests.engine_core.availability_test_support import (
    state_db_path as state_db_path,
)
from tests.waits import THREAD_HANG_GUARD_S

pytestmark = pytest.mark.requirement("LIBM-09")


def _seed_worker_tracks(
    state_db_path: Path,
    tmp_path: Path,
    *,
    count: int,
    prefix: str,
) -> list[str]:
    audio_dir = tmp_path / prefix
    audio_dir.mkdir()
    stable_ids: list[str] = []
    conn = state_db.open_rw(state_db_path)
    try:
        for index in range(count):
            audio = audio_dir / f"{index}.mp3"
            audio.write_bytes(b"\x00")
            stable_id = _stable(f"{prefix}{index}")
            stable_ids.append(stable_id)
            _upsert_track(conn, stable_id, str(audio))
    finally:
        conn.close()
    return stable_ids


def test_worker_retries_busy_lock_instead_of_failed_phase(
    data_dir: Path,
    state_db_path: Path,
    tmp_path: Path,
) -> None:
    _seed_worker_tracks(state_db_path, tmp_path, count=12, prefix="busy-retry-")
    worker = LibraryAvailabilityWorker(data_dir, batch_size=4)
    drain_calls = {"n": 0}
    original_drain_once = worker._drain_once

    def _counting_drain_once() -> None:
        drain_calls["n"] += 1
        return original_drain_once()

    worker._drain_once = _counting_drain_once  # type: ignore[method-assign]

    release_lock = threading.Event()
    stop_contender = threading.Event()

    def _pulse_write_lock() -> None:
        while not stop_contender.is_set():
            holder = state_db.open_rw(state_db_path)
            try:
                holder.execute("BEGIN IMMEDIATE")
                release_lock.wait(timeout=0.15)
                holder.execute("COMMIT")
            finally:
                holder.close()
            time.sleep(0.02)

    contender = threading.Thread(target=_pulse_write_lock, daemon=True)
    contender.start()
    worker.start()
    time.sleep(0.2)
    release_lock.set()
    _wait_worker_complete(worker, guard_s=THREAD_HANG_GUARD_S)
    stop_contender.set()
    contender.join(timeout=2.0)

    snapshot = worker.status()
    assert snapshot.phase != "failed"
    assert snapshot.processed_total > 0
    assert drain_calls["n"] >= 1
    worker.stop()


def test_lock_error_on_first_batch_still_checks_every_row(
    data_dir: Path,
    state_db_path: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A real writer lock through the retry budget must not drop rows.

    Another connection holds ``BEGIN IMMEDIATE`` until the worker logs that
    it gave up the round. Releasing that lock lets the same worker write
    every seeded ``track_availability`` row.
    """
    stable_ids = _seed_worker_tracks(
        state_db_path, tmp_path, count=6, prefix="lock-drain-"
    )
    release = threading.Event()
    holder = _hold_immediate_until(state_db_path, release)
    worker = LibraryAvailabilityWorker(data_dir, batch_size=3)
    try:
        with caplog.at_level("ERROR"):
            worker.start()
            started = time.monotonic()
            while time.monotonic() - started < 40.0:
                if any(
                    "gave up this round" in record.message
                    for record in caplog.records
                ):
                    break
                time.sleep(0.05)
            else:
                pytest.fail("worker never exhausted lock retries under a real lock")
            release.set()
            holder.join(timeout=2.0)
            _wait_worker_complete(worker, guard_s=THREAD_HANG_GUARD_S)
    finally:
        release.set()
        holder.join(timeout=2.0)
        worker.stop()

    conn = state_db.open_rw(state_db_path)
    try:
        rows = conn.execute(
            "SELECT stable_id FROM track_availability WHERE stable_id IN "
            f"({','.join('?' for _ in stable_ids)})",
            stable_ids,
        ).fetchall()
    finally:
        conn.close()
    assert {row[0] for row in rows} == set(stable_ids)


def test_full_probe_resumes_after_lock_exhaustion_when_nothing_is_pending(
    data_dir: Path,
    state_db_path: Path,
    tmp_path: Path,
) -> None:
    """A settled library's full probe must run again after the lock clears."""
    stable_ids = _seed_worker_tracks(
        state_db_path, tmp_path, count=4, prefix="full-lock-"
    )
    worker = LibraryAvailabilityWorker(data_dir, batch_size=4)
    worker.start()
    try:
        _wait_worker_complete(worker, guard_s=THREAD_HANG_GUARD_S)
        before = worker.status()
        assert before.pending == 0
        assert before.phase == "complete"

        conn = state_db.open_rw(state_db_path)
        try:
            checked_before = {
                row[0]: row[1]
                for row in conn.execute(
                    "SELECT stable_id, checked_at FROM track_availability "
                    "WHERE stable_id IN "
                    f"({','.join('?' for _ in stable_ids)})",
                    stable_ids,
                )
            }
        finally:
            conn.close()
        assert set(checked_before) == set(stable_ids)

        release = threading.Event()
        holder = _hold_immediate_until(state_db_path, release)
        try:
            worker.request_probe(full=True)
            started = time.monotonic()
            while time.monotonic() - started < 40.0:
                snapshot = worker.status()
                if (
                    snapshot.pending == 0
                    and snapshot.last_error
                    and "lock retries" in snapshot.last_error
                ):
                    break
                time.sleep(0.05)
            else:
                pytest.fail(
                    "full probe never exhausted lock retries on a settled library: "
                    f"phase={worker.status().phase} pending={worker.status().pending} "
                    f"error={worker.status().last_error}"
                )
            assert worker.status().phase == "queued"
            release.set()
            holder.join(timeout=2.0)

            # Poll status only. Opening another writer in this loop takes
            # the same lock the probe is waiting on and stalls the retry.
            started = time.monotonic()
            while time.monotonic() - started < 30.0:
                done = worker.status()
                if done.phase == "complete" and done.pending == 0:
                    break
                time.sleep(0.05)
            else:
                pytest.fail(
                    "full probe stayed queued after the writer lock was released: "
                    f"phase={worker.status().phase} pending={worker.status().pending} "
                    f"error={worker.status().last_error}"
                )
        finally:
            release.set()
            holder.join(timeout=2.0)

        conn = state_db.open_rw(state_db_path)
        try:
            checked_after = {
                row[0]: row[1]
                for row in conn.execute(
                    "SELECT stable_id, checked_at FROM track_availability "
                    "WHERE stable_id IN "
                    f"({','.join('?' for _ in stable_ids)})",
                    stable_ids,
                )
            }
        finally:
            conn.close()
        assert all(
            checked_after[stable_id] != checked_before[stable_id]
            for stable_id in stable_ids
        )
    finally:
        worker.stop()


def test_mid_round_busy_requeues_priority_ids(
    data_dir: Path,
    state_db_path: Path,
    tmp_path: Path,
) -> None:
    stable_ids = _seed_worker_tracks(
        state_db_path, tmp_path, count=8, prefix="prio-requeue-"
    )
    priority_id = stable_ids[0]
    worker = LibraryAvailabilityWorker(data_dir, batch_size=2)
    worker.request_probe([priority_id])

    requeued: list[list[str]] = []
    original_requeue = worker._requeue_priority_ids

    def _spy_requeue(ids: list[str]) -> None:
        requeued.append(list(ids))
        original_requeue(ids)

    worker._requeue_priority_ids = _spy_requeue  # type: ignore[method-assign]

    stop_holder = threading.Event()
    holder_ready = threading.Event()

    def _hold_write_lock_until_stopped() -> None:
        holder = state_db.open_rw(state_db_path)
        try:
            holder.execute("BEGIN IMMEDIATE")
            holder_ready.set()
            while not stop_holder.wait(timeout=0.05):
                pass
            holder.execute("COMMIT")
        finally:
            holder.close()

    holder = threading.Thread(target=_hold_write_lock_until_stopped, daemon=True)
    holder.start()
    assert holder_ready.wait(timeout=2.0), "contending write lock never acquired"
    worker.start()

    started = time.monotonic()
    while time.monotonic() - started < THREAD_HANG_GUARD_S:
        snapshot = worker.status()
        if snapshot.phase == "queued" and snapshot.last_error and "locked" in snapshot.last_error:
            break
        time.sleep(0.02)
    else:
        stop_holder.set()
        holder.join(timeout=2.0)
        worker.stop()
        pytest.fail("worker never entered queued lock-retry state")

    stop_holder.set()
    holder.join(timeout=2.0)
    _wait_worker_complete(worker, guard_s=THREAD_HANG_GUARD_S)

    assert any(priority_id in batch for batch in requeued)
    conn = state_db.open_rw(state_db_path)
    try:
        row = conn.execute(
            "SELECT state FROM track_availability WHERE stable_id = ?",
            (priority_id,),
        ).fetchone()
        assert row is not None
    finally:
        conn.close()
    worker.stop()
