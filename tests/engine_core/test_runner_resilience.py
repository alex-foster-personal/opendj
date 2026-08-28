"""What the runner does when the STORE is the thing that fails.

sqlite3.Error is not an OSError, so it used to fall straight through every
guard in this module. Two consequences, both permanent:

  - if a store write fails mid-run then the row stays 'running' forever with
    no worker behind it and nothing that will ever revisit it
  - if a store read fails in the drain loop then the supervisor task dies and
    NOTHING claims a queued job again for the life of the process

And one at shutdown:

  - if cancelling an in-flight job raises then the rest of the shutdown never
    runs, so the db handle and hub binding leak on the way out

The failures are injected on a real JobStore with real subprocess workers,
because the point is what the runner does with an error, not that an error
can be constructed.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from apps.engine_core.jobs.reap import group_has_live_member
from apps.engine_core.jobs.runner import (
    JobRunner,
    register_worker,
    unregister_worker,
)
from apps.engine_core.jobs.store import JobConflict, JobStore

# Prints one progress line, then lingers just long enough that the runner is
# genuinely mid-run when the injected store error fires.
_CHATTY_WORKER = (
    "import json, sys, time\n"
    "print(json.dumps({'progress': 0.5, 'message': 'half'}), flush=True)\n"
    "time.sleep(0.3)\n"
)
_SLEEPER = "import time; time.sleep(600)"


def _store(tmp_path: Path) -> JobStore:
    store = JobStore(tmp_path / "jobs.db", boot_id="boot-a", owner_pid=os.getpid())
    store.recover()
    return store


@pytest.fixture
def kinds() -> Iterator[None]:
    register_worker("chatty", lambda _p: [sys.executable, "-c", _CHATTY_WORKER])
    register_worker("sleeper", lambda _p: [sys.executable, "-c", _SLEEPER])
    yield
    unregister_worker("chatty")
    unregister_worker("sleeper")


async def _await_status(
    store: JobStore, job_id: str, wanted: set[str], timeout_s: float = 20.0
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        row = store.get(job_id)
        if row["status"] in wanted:
            return row
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                f"job {job_id} stuck at {row['status']!r} (wanted {wanted}); "
                f"error={row['error']!r}"
            )
        await asyncio.sleep(0.02)


async def _await_pgid(
    store: JobStore, job_id: str, timeout_s: float = 20.0
) -> int:
    """The recorded worker pgid, once the spawn has persisted it."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        pgid = store.get(job_id)["worker_pgid"]
        if pgid is not None:
            return int(pgid)
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"job {job_id} never recorded a worker pgid")
        await asyncio.sleep(0.02)


def test_a_store_failure_mid_run_fails_the_row(tmp_path: Path, kinds: None) -> None:
    """A row whose run blew up must end terminal, not sit 'running' forever."""
    store = _store(tmp_path)

    def _boom(_job_id: str, _progress: float, _message: str | None) -> dict:
        raise sqlite3.OperationalError("database is locked")

    store.set_progress = _boom  # type: ignore[method-assign]

    async def drive() -> dict[str, Any]:
        runner = JobRunner(store, poll_s=0.01)
        await runner.start()
        job = store.enqueue("chatty", {})
        try:
            return await _await_status(store, job["id"], {"failed", "succeeded"})
        finally:
            await runner.stop()

    row = asyncio.run(drive())
    assert row["status"] == "failed", row
    assert "OperationalError" in row["error"], row["error"]
    assert "database is locked" in row["error"], row["error"]
    store.close()


def test_the_supervisor_survives_a_failed_drain(tmp_path: Path, kinds: None) -> None:
    """One transient store error must not end queue supervision permanently.

    Before, the sqlite error escaped _supervise and killed the task: no
    later job was ever claimed, for the whole life of the process.
    """
    store = _store(tmp_path)
    real_claim = store.claim_queued
    drains = {"failures_left": 1}

    def _flaky(*, limit: int = 1) -> list[dict[str, Any]]:
        if drains["failures_left"] > 0:
            drains["failures_left"] -= 1
            raise sqlite3.OperationalError("database is locked")
        return real_claim(limit=limit)

    store.claim_queued = _flaky  # type: ignore[method-assign]

    async def drive() -> dict[str, Any]:
        runner = JobRunner(store, poll_s=0.01)
        await runner.start()
        job = store.enqueue("chatty", {})
        try:
            return await _await_status(store, job["id"], {"succeeded", "failed"})
        finally:
            await runner.stop()

    row = asyncio.run(drive())
    assert drains["failures_left"] == 0, "the drain failure never fired"
    assert row["status"] == "succeeded", row
    store.close()


def test_shutdown_completes_when_a_cancel_conflicts(
    tmp_path: Path, kinds: None
) -> None:
    """A row that goes terminal mid-shutdown is skipped, not fatal.

    stop() used to let JobConflict out, which abandoned every remaining job
    AND the caller's close()/unbind() -- see the app lifespan.
    """
    store = _store(tmp_path)

    async def drive() -> tuple[int, dict[str, Any]]:
        runner = JobRunner(store, poll_s=0.01)
        await runner.start()
        job = store.enqueue("sleeper", {})
        row = await _await_status(store, job["id"], {"running"})
        pgid = await _await_pgid(store, job["id"])

        # The row goes terminal behind the runner's back, exactly as a racing
        # _run or a second cancel would leave it. begin_cancel now refuses it.
        store.finish(job["id"], "failed", error="raced to a terminal status")
        with pytest.raises(JobConflict):
            await runner.cancel(job["id"])

        await runner.stop()  # must NOT raise, and must not block forever
        assert row["id"] == job["id"]
        return pgid, store.get(job["id"])

    pgid, final = asyncio.run(drive())
    # The status is left exactly as it was found: the row's outcome is settled
    # and shutdown has no business rewriting it.
    assert final["status"] == "failed", final
    assert final["error"] == "raced to a terminal status", final
    # ...but the worker behind it is NOT left running. Tolerating the status
    # conflict must never mean tolerating an orphan.
    assert not group_has_live_member(pgid), (
        f"shutdown left process group {pgid} alive after skipping the row"
    )
    store.close()


def test_shutdown_cancels_a_live_worker_and_proves_it_dead(
    tmp_path: Path, kinds: None
) -> None:
    """The happy path stop() must still take the whole tree with it."""
    store = _store(tmp_path)

    async def drive() -> dict[str, Any]:
        runner = JobRunner(store, poll_s=0.01)
        await runner.start()
        job = store.enqueue("sleeper", {})
        await _await_status(store, job["id"], {"running"})
        await runner.stop()
        return store.get(job["id"])

    row = asyncio.run(drive())
    assert row["status"] == "cancelled", row
    store.close()
