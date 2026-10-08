"""The finish observer: a kind hears its own run's verdict, and only that.

Real JobStore, real JobRunner, real forked workers (tiny ``python -c``
scripts). Nothing in the runner is stubbed.

Single-line intent:
  - if a nonzero exit or a malformed line does not reach the observer then a
    kind's failure bookkeeping (the coverage ledger) never hears of it
  - if a cancel reaches the observer then a user's stop reads as a failure
  - if an observer that raises changes the row then bookkeeping can fail work
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from apps.engine_core.jobs.runner import (
    JobRunner,
    register_finish_observer,
    register_worker,
    unregister_worker,
)
from apps.engine_core.jobs.store import JobStore

KIND = "finish-observer-test"
TERMINAL = {"succeeded", "failed", "cancelled", "unknown"}
SCRIPTS = {
    "exit0": "pass",
    "exit3": "import sys; sys.exit(3)",
    # Exits 0, so only the malformed line can make this run fail.
    "garbage": "print('not json', flush=True)",
    "sleep": "import time; time.sleep(600)",
}


@pytest.fixture
def seen() -> Iterator[list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    register_worker(KIND, lambda p: [sys.executable, "-c", SCRIPTS[p["script"]]])
    register_finish_observer(KIND, rows.append)
    yield rows
    unregister_worker(KIND)


def _store(tmp_path: Path) -> JobStore:
    store = JobStore(tmp_path / "jobs.db", boot_id="boot-a", owner_pid=os.getpid())
    store.recover()
    return store


async def _until(store: JobStore, job_id: str, wanted: set[str]) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + 20
    while True:
        row = store.get(job_id)
        if row["status"] in wanted and (row["status"] != "running" or row["worker_pgid"]):
            return row
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"job never reached {wanted}: {row}")
        await asyncio.sleep(0.02)


def _run(store: JobStore, script: str) -> dict[str, Any]:
    async def drive() -> dict[str, Any]:
        runner = JobRunner(store, poll_s=0.01)
        await runner.start()
        job = store.enqueue(KIND, {"script": script})
        await _until(store, job["id"], TERMINAL)
        # stop() gathers the run task, so the observer has finished by now.
        await runner.stop()
        return store.get(job["id"])

    return asyncio.run(drive())


@pytest.mark.parametrize("script", ["exit3", "garbage"])
def test_a_failed_run_reaches_the_observer_with_its_failed_row(
    tmp_path: Path, seen: list[dict[str, Any]], script: str
) -> None:
    store = _store(tmp_path)
    final = _run(store, script)
    assert final["status"] == "failed", final
    assert ("worker exited 3" if script == "exit3" else "not JSON") in final["error"]
    assert [(row["id"], row["status"]) for row in seen] == [(final["id"], "failed")]
    assert seen[0]["payload"] == {"script": script}
    store.close()


def test_a_succeeded_run_reaches_the_observer_as_succeeded(
    tmp_path: Path, seen: list[dict[str, Any]]
) -> None:
    store = _store(tmp_path)
    final = _run(store, "exit0")
    assert final["status"] == "succeeded", final
    assert [row["status"] for row in seen] == ["succeeded"]
    store.close()


def test_a_cancelled_run_never_reaches_the_observer(
    tmp_path: Path, seen: list[dict[str, Any]]
) -> None:
    store = _store(tmp_path)

    async def drive() -> dict[str, Any]:
        runner = JobRunner(store, poll_s=0.01)
        await runner.start()
        job = store.enqueue(KIND, {"script": "sleep"})
        await _until(store, job["id"], {"running"})
        final = await runner.cancel(job["id"])
        await runner.stop()
        return final

    final = asyncio.run(drive())
    assert final["status"] == "cancelled", final
    assert seen == []
    store.close()


def test_an_observer_that_raises_leaves_the_verdict_alone(tmp_path: Path) -> None:
    calls: list[str] = []

    def boom(job: dict[str, Any]) -> None:
        calls.append(job["status"])
        raise RuntimeError("bookkeeping exploded")

    register_worker(KIND, lambda p: [sys.executable, "-c", SCRIPTS[p["script"]]])
    register_finish_observer(KIND, boom)
    try:
        store = _store(tmp_path)
        failed = _run(store, "exit3")
        succeeded = _run(store, "exit0")
        store.close()
    finally:
        unregister_worker(KIND)
    assert calls == ["failed", "succeeded"]
    assert failed["status"] == "failed" and "worker exited 3" in failed["error"]
    assert succeeded["status"] == "succeeded" and succeeded["error"] is None


def test_stop_joins_a_slow_observer_and_its_slot_is_free_meanwhile(tmp_path: Path) -> None:
    """[if] an observer outlasts shutdown's spawn settle [then] stop() still waits for it, [else stop].

    An observer awaited inside the job's own task left the job in ``_tasks``
    with no worker in ``_running``: shutdown's settle read that as a fork,
    cancelled it, and returned while the observer's thread kept writing.
    Control the other way: the observer must not hold the job's slot.
    """
    release = threading.Event()
    entered = threading.Event()
    finished: list[str] = []

    def slow(job: dict[str, Any]) -> None:
        entered.set()
        release.wait(10)
        time.sleep(0.3)  # still writing after release, longer than the settle
        finished.append(job["status"])

    register_worker(KIND, lambda p: [sys.executable, "-c", SCRIPTS[p["script"]]])
    register_finish_observer(KIND, slow)
    try:
        store = _store(tmp_path)

        async def drive() -> tuple[int, list[str]]:
            runner = JobRunner(store, poll_s=0.01, spawn_settle_s=0.05)
            await runner.start()
            job = store.enqueue(KIND, {"script": "exit3"})
            await _until(store, job["id"], TERMINAL)
            while not entered.is_set():
                await asyncio.sleep(0.01)
            # The worker has settled, so its slot is free while the observer runs.
            in_flight = runner._in_flight()
            release.set()
            await runner.stop()
            # Read before asyncio.run's own teardown joins the executor's
            # threads, which would hide an observer stop() left running.
            return in_flight, list(finished)

        in_flight, at_stop = asyncio.run(drive())
        store.close()
    finally:
        release.set()
        unregister_worker(KIND)
    assert in_flight == 0
    assert at_stop == ["failed"]
