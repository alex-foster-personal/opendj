"""Cancelling a worker that just died must land 'cancelled', not 'unknown'.

reap.py already states the distinction its own callers have to honour:
``process_group_exists`` answers yes for a group whose every member is a
zombie, because the pgid stays allocated until somebody waits, and
``group_has_live_member`` is the predicate for "is anything still RUNNING in
there". cancel() used the first one to decide whether the kill worked, so a
worker that died a moment before the cancel arrived reported "could not
confirm the group is dead" and the row ended 'unknown' (C10).

That is not a cosmetic mislabel. 'unknown' is the status that refuses to
re-enqueue without a reconcile hook, so a perfectly ordinary cancel of a
just-finished job left a row a human had to resolve by hand.

The zombie group here is REAL. Its single member is a process that has exited
and whose parent -- this test -- has deliberately not waited on it, which is
what keeps the group in that state for the whole test rather than for the few
microseconds an orphan survives before init collects it.

Single-line intent:
  - if cancel treats a zombie-only group as alive then cancelling a worker
    that just died reports 'unknown' and blocks its own re-enqueue
  - if the fixture's group is not actually zombie-only then the test proves
    nothing, so the shape is asserted before it is used
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from apps.engine_core.jobs.reap import (
    WorkerIdentity,
    group_has_live_member,
    process_group_exists,
)
from apps.engine_core.jobs.runner import JobRunner, _Worker
from apps.engine_core.jobs.store import JobStore


@pytest.fixture
def zombie_pgid() -> Iterator[int]:
    """A live pgid whose only member is a zombie, held stable by this process.

    The child leads its own session (pgid == pid) and exits immediately. It is
    never waited on, so it stays a zombie: still a member of its group, so the
    pgid stays allocated, but nothing of it is running.
    """
    corpse = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 10
    while group_has_live_member(corpse.pid):
        if time.monotonic() >= deadline:
            raise AssertionError("the child never finished exiting")
        time.sleep(0.02)
    try:
        yield corpse.pid
    finally:
        corpse.wait(timeout=10)


def test_the_fixture_really_builds_a_zombie_only_group(zombie_pgid: int) -> None:
    """Guard the setup: this is the exact shape C10 mishandled."""
    assert process_group_exists(zombie_pgid), "the pgid must still be allocated"
    assert not group_has_live_member(zombie_pgid), "nothing may still be running"


def test_cancelling_a_worker_that_just_died_lands_cancelled(
    tmp_path: Path, zombie_pgid: int
) -> None:
    """A zombie is a dead process, so the cancel it ends is a real cancel.

    The runner is handed a job whose recorded group is the zombie-only one and
    a worker process that has already exited -- the state a cancel arriving
    one instant late genuinely finds. Before the fix the group "existed", so
    cancel wrote 'unknown' with "could not confirm the group is dead".
    """
    store = JobStore(tmp_path / "jobs.db", boot_id="boot-a", owner_pid=os.getpid())
    store.recover()

    async def drive() -> dict[str, Any]:
        runner = JobRunner(store, poll_s=0.01)
        job = store.enqueue("just-died", {})
        store.claim_queued()
        # A real, already-exited process stands in for the dead worker: cancel
        # waits on proc, and waiting on a corpse must not hang the test.
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "pass",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        identity = WorkerIdentity(
            pgid=zombie_pgid,
            argv=(sys.executable, "-c", "pass"),
            started_at=time.time(),
        )
        store.record_worker(job["id"], pid=proc.pid, identity=identity)
        runner._running[job["id"]] = _Worker(proc=proc, identity=identity)
        return await runner.cancel(job["id"])

    final = asyncio.run(drive())

    assert final["status"] == "cancelled", (
        f"a zombie-only group was read as alive: {final['error']}"
    )
    assert "could not confirm" not in (final["error"] or ""), final["error"]
    assert not group_has_live_member(zombie_pgid)
    store.close()
