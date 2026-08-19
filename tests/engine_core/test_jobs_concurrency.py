"""max_concurrent is a limit, not a suggestion.

The supervisor sized each drain from ``len(self._running)``, but a job only
lands in ``_running`` once ``_run`` has got past ``create_subprocess_exec`` --
several milliseconds of fork and exec later, and several supervisor polls
later. Every poll inside that window saw a free slot that was already spoken
for and claimed another row, so a runner told to run one job at a time ran
however many rows the queue held (C11).

That is a real resource bug, not an accounting one: max_concurrent is what
stops N stem separations or N analysis passes from being started at once on a
machine sized for one.

The proof is mutual exclusion enforced by the OS, not by polling the store: a
worker takes an O_EXCL slot file and refuses to run if one already exists, so
an overlap is recorded by the worker that lost rather than inferred by a
sampler that has to be lucky.

Single-line intent:
  - if the slot is reserved only when the worker registers then the supervisor
    over-claims for the whole width of the fork+exec window
  - if two workers ever hold the slot at once then the second exits nonzero and
    its row fails, so a green run is a run that respected the limit
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from apps.engine_core.jobs.runner import (
    JobRunner,
    register_worker,
    unregister_worker,
)
from apps.engine_core.jobs.store import JobStore

# O_EXCL is the whole test: creating the slot is atomic, so a second worker
# that overlaps the first cannot fail to notice. It holds the slot long enough
# that any over-claim within the spawn window is guaranteed to collide.
_SLOT_WORKER = (
    "import json, os, sys, time\n"
    "slot = sys.argv[1]\n"
    "try:\n"
    "    fd = os.open(slot, os.O_CREAT | os.O_EXCL | os.O_WRONLY)\n"
    "except FileExistsError:\n"
    "    sys.stderr.write('CONCURRENCY VIOLATION: the slot was already taken')\n"
    "    sys.exit(3)\n"
    "print(json.dumps({'progress': 0.5, 'message': 'holding the slot'}),"
    " flush=True)\n"
    "time.sleep(0.25)\n"
    "os.close(fd)\n"
    "os.unlink(slot)\n"
)

_JOBS: int = 5


@pytest.fixture
def kinds() -> Iterator[None]:
    register_worker(
        "slot-holder",
        lambda p: [sys.executable, "-c", _SLOT_WORKER, str(p["slot"])],
    )
    yield
    unregister_worker("slot-holder")


def test_max_concurrent_is_never_exceeded_during_the_spawn_window(
    tmp_path: Path, kinds: None
) -> None:
    """One slot, five jobs: every one must succeed, in sequence.

    poll_s is deliberately far shorter than a fork+exec, so the supervisor
    polls several times inside the window a claimed-but-unregistered job
    occupies. That window is the bug; a run where every row succeeded is a run
    where no second worker ever found the slot taken.
    """
    store = JobStore(tmp_path / "jobs.db", boot_id="boot-a", owner_pid=os.getpid())
    store.recover()
    slot = tmp_path / "slot"

    async def drive() -> list[dict]:
        runner = JobRunner(store, poll_s=0.001, max_concurrent=1)
        await runner.start()
        ids = [
            store.enqueue("slot-holder", {"slot": str(slot)})["id"]
            for _ in range(_JOBS)
        ]
        deadline = asyncio.get_running_loop().time() + 60
        try:
            while True:
                rows = [store.get(job_id) for job_id in ids]
                if all(row["status"] in {"succeeded", "failed"} for row in rows):
                    return rows
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError(f"jobs never settled: {rows}")
                await asyncio.sleep(0.02)
        finally:
            await runner.stop()

    rows = asyncio.run(drive())

    violations = [row for row in rows if "CONCURRENCY VIOLATION" in (row["error"] or "")]
    assert not violations, (
        f"{len(violations)} of {_JOBS} workers ran while the single slot was "
        f"already held: {violations[0]['error']}"
    )
    assert all(row["status"] == "succeeded" for row in rows), rows
    assert not slot.exists(), "a worker left the slot behind"
    store.close()


def test_the_supervisor_reserves_the_slot_before_the_worker_registers(
    tmp_path: Path, kinds: None
) -> None:
    """The accounting itself, without waiting on the OS to expose it.

    A spawned-but-unregistered job is invisible to ``_running`` by
    construction, so counting that is what let the next poll double-claim.
    The count the supervisor sizes its drain from has to include every job it
    has already handed to a task.
    """
    store = JobStore(tmp_path / "jobs.db", boot_id="boot-a", owner_pid=os.getpid())
    store.recover()
    slot = tmp_path / "slot"

    async def drive() -> tuple[int, int]:
        runner = JobRunner(store, poll_s=10.0, max_concurrent=1)
        for _ in range(_JOBS):
            store.enqueue("slot-holder", {"slot": str(slot)})
        # One drain, by hand, at the exact instant the bug fired: the tasks
        # exist but not one of them has run a single line yet.
        for job in store.claim_queued(limit=runner.max_concurrent):
            runner._spawn(job)
        in_flight = runner._in_flight()
        registered = len(runner._running)
        for task in list(runner._tasks.values()):
            task.cancel()
        await asyncio.gather(*runner._tasks.values(), return_exceptions=True)
        return in_flight, registered

    in_flight, registered = asyncio.run(drive())

    assert registered == 0, "the premise is wrong: a worker registered too early"
    assert in_flight == 1, (
        "the spawned job is not counted against max_concurrent, so the next "
        "poll claims another one"
    )
    store.close()
