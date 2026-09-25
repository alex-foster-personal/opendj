"""A chatty worker must not stall the engine loop (issue #3964).

The folder import writes one progress line per FILE. The runner used to write
each line to jobs.db synchronously on the event loop, and a StreamReader with
buffered data hands back line after line without ever yielding. So every line
already sitting in the pipe cost one sqlite commit (one fsync, in WAL mode)
with the loop blocked, and on agentbox's CI-shared disk a 10,000-file import
held /api/v1/health unanswered for about two minutes.

The latency is injected on a REAL JobStore, the same way
test_runner_resilience.py injects failures: the rows are real, the worker is a
real subprocess, and the delay stands in for the fsync cost the issue measured
(178.1 s over 10,000 files, so up to ~18 ms per write). A fast local disk hides
the defect entirely, which is exactly why the model is needed here.

Single-line intent:
  - if a worker emits hundreds of progress lines against a slow disk then the
    engine loop never goes more than LOOP_GAP_BOUND_S without running
    [broken if progress writes run on the loop, per #3964]
  - if progress is coalesced then it is still reported DURING the run, the
    latest line wins, and the final row reads 1.0
    [broken if the fix only writes at EOF, or keeps a stale line]
  - if a kind has a progress observer then it still sees every line, in
    order, after a write that covers it [broken if coalescing drops lines]
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from apps.engine_core.jobs.runner import (
    PROGRESS_WRITE_INTERVAL_S,
    JobRunner,
    register_progress_observer,
    register_worker,
    unregister_worker,
)
from apps.engine_core.jobs.store import JobStore

pytestmark = pytest.mark.requirement("SETUP-19")

KIND = "test.chatty-progress"
LINES = 600
# The worker paces itself so the run lasts ~1.5 s: long enough that a
# coalesced writer must report mid-run, short enough for a unit test.
LINE_PACE_S = 0.0025
# Per-write disk cost modelled on the #3964 measurement (see module doc).
WRITE_COST_S = 0.010
# The stated bound. Pre-fix this run measures several SECONDS: 600 lines x
# 10 ms, most of them processed back to back without a yield.
LOOP_GAP_BOUND_S = 0.5
HEARTBEAT_S = 0.01

_WORKER = (
    "import json, sys, time\n"
    f"n = {LINES}\n"
    "for i in range(1, n + 1):\n"
    "    print(json.dumps({'progress': i / n, 'message': f'file {i}', "
    "'seq': i}), flush=True)\n"
    f"    time.sleep({LINE_PACE_S})\n"
)


@dataclass
class _Write:
    progress: float
    message: str | None
    thread: int
    at: float


class _SlowDiskJobStore(JobStore):
    """A real JobStore whose progress commit costs what a busy disk charges."""

    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path, boot_id="boot-coalesce", owner_pid=os.getpid())
        self.progress_writes: list[_Write] = []

    def set_progress(
        self, job_id: str, progress: float, message: str | None
    ) -> dict[str, Any]:
        self.progress_writes.append(
            _Write(progress, message, threading.get_ident(), time.monotonic())
        )
        time.sleep(WRITE_COST_S)
        return super().set_progress(job_id, progress, message)


@dataclass
class _Run:
    row: dict[str, Any]
    max_loop_gap_s: float
    loop_thread: int
    started: float
    ended: float
    observed: list[tuple[dict[str, Any], dict[str, Any]]] = field(
        default_factory=list
    )


@pytest.fixture(scope="module")
def observed() -> Iterator[list[tuple[dict[str, Any], dict[str, Any]]]]:
    sink: list[tuple[dict[str, Any], dict[str, Any]]] = []
    register_worker(KIND, lambda _p: [sys.executable, "-c", _WORKER])
    register_progress_observer(KIND, lambda job, line: sink.append((job, line)))
    yield sink
    unregister_worker(KIND)


def _drive(store: _SlowDiskJobStore, sink: list[Any]) -> _Run:
    async def run() -> _Run:
        loop_thread = threading.get_ident()
        runner = JobRunner(store, poll_s=0.01)
        await runner.start()
        started = time.monotonic()
        job = store.enqueue(KIND, {})
        max_gap = 0.0
        deadline = started + 60.0
        try:
            while True:
                tick = time.monotonic()
                await asyncio.sleep(HEARTBEAT_S)
                max_gap = max(max_gap, time.monotonic() - tick - HEARTBEAT_S)
                row = await asyncio.to_thread(store.get, job["id"])
                if row["status"] in {"succeeded", "failed", "unknown"}:
                    break
                if time.monotonic() > deadline:
                    raise AssertionError(f"job never finished: {row}")
        finally:
            await runner.stop()
        return _Run(
            row=row,
            max_loop_gap_s=max_gap,
            loop_thread=loop_thread,
            started=started,
            ended=time.monotonic(),
            observed=sink,
        )

    return asyncio.run(run())


@pytest.fixture(scope="module")
def run(
    tmp_path_factory: pytest.TempPathFactory, observed: list[Any]
) -> Iterator[tuple[_Run, _SlowDiskJobStore]]:
    """One run shared by every assertion below: each costs seconds."""
    store = _SlowDiskJobStore(tmp_path_factory.mktemp("coalesce") / "jobs.db")
    store.recover()
    try:
        yield _drive(store, observed), store
    finally:
        store.close()


def test_the_loop_keeps_running_while_a_worker_floods_progress(
    run: tuple[_Run, _SlowDiskJobStore],
) -> None:
    result, _store = run
    assert result.row["status"] == "succeeded", result.row
    assert result.max_loop_gap_s < LOOP_GAP_BOUND_S, (
        f"the engine loop stalled {result.max_loop_gap_s:.3f}s during the "
        f"run (bound {LOOP_GAP_BOUND_S}s); every route waits that long"
    )


def test_no_progress_line_is_written_on_the_loop_thread(
    run: tuple[_Run, _SlowDiskJobStore],
) -> None:
    result, store = run
    line_writes = [w for w in store.progress_writes if w.message != "done"]
    assert line_writes, "no progress line was ever written"
    on_loop = [w for w in line_writes if w.thread == result.loop_thread]
    assert not on_loop, (
        f"{len(on_loop)} of {len(line_writes)} progress writes ran on the "
        "event loop thread"
    )


def test_progress_writes_are_rate_bounded_but_still_reported_mid_run(
    run: tuple[_Run, _SlowDiskJobStore],
) -> None:
    result, store = run
    line_writes = [w for w in store.progress_writes if w.message != "done"]
    elapsed = result.ended - result.started
    ceiling = int(elapsed / PROGRESS_WRITE_INTERVAL_S) + 2
    assert len(line_writes) <= ceiling, (
        f"{len(line_writes)} progress writes for {LINES} lines in "
        f"{elapsed:.2f}s; the rate bound allows {ceiling}"
    )
    # Overshoot control: writing only at EOF would pass the bound above.
    mid_run = [w for w in line_writes if w.progress < 1.0]
    assert len(mid_run) >= 2, (
        f"only {len(mid_run)} progress writes landed before the last line; "
        "progress must still be reported while the job runs"
    )
    progresses = [w.progress for w in line_writes]
    assert progresses == sorted(progresses), progresses
    # The coalesced write carries the LATEST line, so the last one written
    # before 'done' is the worker's final line, not a stale earlier one.
    assert line_writes[-1].message == f"file {LINES}", line_writes[-1]
    assert result.row["progress"] == 1.0, result.row
    assert result.row["message"] == "done", result.row


def test_the_observer_still_sees_every_line_after_a_covering_write(
    run: tuple[_Run, _SlowDiskJobStore],
) -> None:
    result, _store = run
    seqs = [line["seq"] for _job, line in result.observed]
    assert seqs == list(range(1, LINES + 1)), (
        f"observer saw {len(seqs)} lines, first gaps at "
        f"{[s for a, s in zip(range(1, LINES + 1), seqs, strict=False) if a != s][:5]}"
    )
    uncovered = [
        (job["progress"], line["progress"])
        for job, line in result.observed
        if job["progress"] < line["progress"]
    ]
    assert not uncovered, (
        f"{len(uncovered)} lines were observed before the store recorded "
        f"them, e.g. {uncovered[:3]}"
    )
