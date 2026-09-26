"""A failing run's terminal write is the last word on its row (PR #4003).

The coalescing writer (issue #3964) commits progress in a worker thread. When
the run fails mid-write, the runner aborts the writer and then writes the
terminal status. Cancelling the task that awaits a thread only stops the
WAITING: the thread still commits, and could land its progress update, plus
its job event, after the row was already failed. abort must therefore let an
in-flight write finish before it returns.

The store is a real JobStore; the in-flight write is held open by a gate so
the window the race needs is deterministic instead of a matter of timing.

Single-line intent:
  - [if] a run fails mid progress write [then] its failure is the row's last write, [else stop]
  - if a progress write is in flight when the run fails then abort returns
    only after it lands, so the terminal write follows it
    [broken if abort cancels without waiting for the thread]
  - if lines are still queued when the run fails then abort does not write
    them [broken if abort turns into a flush]
  - if nothing is in flight then abort returns promptly
    [broken if abort waits out the write interval or hangs]
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import pytest

from apps.engine_core.jobs.runner import JobRunner, _ProgressWriter
from apps.engine_core.jobs.store import JobStore

pytestmark = pytest.mark.requirement("SETUP-19")

# How long the gated write stays in its thread. abort must wait this out.
WRITE_HELD_S = 0.3
# Far above an idle abort's cost, far below the write interval under test.
IDLE_ABORT_BOUND_S = 0.1
WRITE_INTERVAL_S = 5.0


class _GatedJobStore(JobStore):
    """A real JobStore whose progress write blocks until the test opens it."""

    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path, boot_id="boot-abort", owner_pid=os.getpid())
        self.gate = threading.Event()
        self.write_started = threading.Event()
        self.log: list[tuple[str, Any]] = []

    def set_progress(self, job_id: str, progress: float, message: str | None) -> dict[str, Any]:
        self.write_started.set()
        self.gate.wait()
        row = super().set_progress(job_id, progress, message)
        self.log.append(("progress", message))
        return row

    def finish(self, job_id: str, status: str, *, error: str | None = None) -> dict[str, Any]:
        row = super().finish(job_id, status, error=error)
        self.log.append(("finish", status))
        return row


def _running_job(store: JobStore) -> str:
    job_id = store.enqueue("test.progress-abort", {})["id"]
    store.claim_queued()
    # Read back through get(): JobStore defines a method named `list`, so mypy
    # resolves claim_queued's `list[...]` return type to that method.
    assert store.get(job_id)["status"] == "running"
    return job_id


async def _fail_during_write(store: _GatedJobStore, job_id: str) -> float:
    """Emulate _pump's failure path while a write is in its thread."""
    writer = _ProgressWriter(store, job_id, WRITE_INTERVAL_S)
    writer.offer(0.1, "file 1", {})
    await asyncio.to_thread(store.write_started.wait, 5)
    assert store.write_started.is_set(), "the first write never started"
    # Queued behind the in-flight write; a failing run must not flush these.
    writer.offer(0.2, "file 2", {})
    writer.offer(0.3, "file 3", {})
    asyncio.get_running_loop().call_later(WRITE_HELD_S, store.gate.set)
    started = time.monotonic()
    await writer.abort()
    waited = time.monotonic() - started
    store.finish(job_id, "failed", error="worker blew up")
    # Room for a stray write to land if abort left one behind.
    await asyncio.sleep(WRITE_HELD_S)
    return waited


@pytest.fixture
def store(tmp_path: Path) -> _GatedJobStore:
    return _GatedJobStore(tmp_path / "jobs.db")


def test_abort_waits_for_in_flight_write_before_terminal_write(
    store: _GatedJobStore,
) -> None:
    job_id = _running_job(store)
    waited = asyncio.run(_fail_during_write(store, job_id))
    assert store.log[-1] == ("finish", "failed"), (
        f"a progress write landed after the terminal write: {store.log}"
    )
    assert ("progress", "file 1") in store.log, store.log
    assert waited >= WRITE_HELD_S * 0.8, (
        f"abort returned after {waited:.3f}s, before the held write landed"
    )
    row = store.get(job_id)
    assert row["status"] == "failed" and row["message"] == "file 1", row


def test_abort_does_not_flush_queued_lines(store: _GatedJobStore) -> None:
    job_id = _running_job(store)
    asyncio.run(_fail_during_write(store, job_id))
    written = [message for kind, message in store.log if kind == "progress"]
    assert written == ["file 1"], f"abort flushed queued lines: {written}"


def test_abort_with_nothing_in_flight_returns_promptly(
    store: _GatedJobStore,
) -> None:
    job_id = _running_job(store)
    store.gate.set()

    async def scenario() -> float:
        writer = _ProgressWriter(store, job_id, WRITE_INTERVAL_S)
        await asyncio.sleep(0)
        started = time.monotonic()
        await writer.abort()
        return time.monotonic() - started

    waited = asyncio.run(scenario())
    assert waited < IDLE_ABORT_BOUND_S, f"idle abort took {waited:.3f}s"
    assert store.log == [], store.log


# ---------------------------------------------------------------------------
# A failed write fails the run at once, not at the worker's next line or EOF.
#
#   - [if] a progress write fails while the worker is quiet [then] the pump raises within
#     FAIL_FAST_BOUND_S [broken if the pump only notices on the next line or at EOF]
#   - [if] writes succeed and the worker goes quiet [then] the pump waits for EOF and
#     returns cleanly [broken if the race treats a quiet worker as a failure]

QUIET_WORKER_S = 30.0
FAIL_FAST_BOUND_S = 5.0
_ONE_LINE_THEN_QUIET = (
    "import json, sys, time; "
    "print(json.dumps({'progress': 0.1, 'message': 'file 1'}), flush=True); "
    "time.sleep(float(sys.argv[1]))"
)


class _RefusingJobStore(JobStore):
    """A real JobStore whose disk has started refusing progress writes."""

    def set_progress(self, job_id: str, progress: float, message: str | None) -> dict[str, Any]:
        raise sqlite3.OperationalError("disk I/O error")


async def _pump_one_line_then_quiet(store: JobStore, job_id: str, quiet_s: float) -> str | None:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _ONE_LINE_THEN_QUIET,
        str(quiet_s),
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        return await asyncio.wait_for(
            JobRunner(store)._pump(job_id, proc, deque(maxlen=8)), FAIL_FAST_BOUND_S
        )
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()


def test_a_failed_write_fails_the_pump_while_the_worker_is_quiet(tmp_path: Path) -> None:
    store = _RefusingJobStore(tmp_path / "jobs.db", boot_id="boot-refuse", owner_pid=os.getpid())
    job_id = _running_job(store)
    started = time.monotonic()
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        asyncio.run(_pump_one_line_then_quiet(store, job_id, QUIET_WORKER_S))
    assert time.monotonic() - started < FAIL_FAST_BOUND_S


def test_a_quiet_worker_with_healthy_writes_still_runs_to_eof(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.db", boot_id="boot-quiet", owner_pid=os.getpid())
    job_id = _running_job(store)
    assert asyncio.run(_pump_one_line_then_quiet(store, job_id, 0.5)) is None
    assert store.get(job_id)["message"] == "file 1"
