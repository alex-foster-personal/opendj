"""The window between the fork and the worker_pgid write, said out loud.

``worker_pgid`` is the ONLY thing that can ever name a worker process again:
boot recovery reaps what the rows name, and cancel reaps what the runner
holds. Between ``create_subprocess_exec`` returning and that column being
written there is a live process nothing can identify, and the two places that
report the aftermath both used to describe it as if it were nothing:

  - boot recovery said "no worker pgid recorded; nothing to reap", which is
    true only if the spawn never happened -- if it did, a worker is still out
    there and this sentence is the reason nobody goes looking (C16);
  - cancel said "this engine holds no worker for the row", which names the
    engine's bookkeeping rather than the condition that produced it.

So the window is closed as far as it can be (the write is the first thing
after the fork, before anything may yield) and named where it cannot be.

Single-line intent:
  - if the pgid write is not the first thing after the fork then any yield
    inserted before it widens a window that nothing can clean up after
  - if the orphan message sounds benign then a row that may have a live worker
    behind it reads as a row that never started
  - if shutdown only cancels the workers it already HOLDS then a job still
    inside the window is never cancelled, and the shutdown then blocks until
    that worker finishes by itself
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from apps.engine_core.jobs.runner import (
    JobRunner,
    register_worker,
    unregister_worker,
)
from apps.engine_core.jobs.store import JOBS_TOPIC, JobStore
from apps.shared.events import set_hub

# Writes nothing and never exits: the row's only content is what the runner
# put there, so the publish order is the runner's alone.
_MUTE = "import time; time.sleep(600)"

# Short enough that a shutdown which fails to cancel it still lets go of the
# machine on its own, rather than wedging the suite for ten minutes -- which
# is exactly what the unfixed shutdown did.
_BRIEF = "import time; time.sleep(20)"

# A fork+exec is milliseconds. A shutdown that has not finished well inside
# this is not slow, it is waiting on a worker it failed to cancel.
_SHUTDOWN_BUDGET_S: float = 8.0


class _PublishLog:
    """Every jobs publish, in order, as (status, pgid-was-recorded)."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, bool]] = []

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        if topic == JOBS_TOPIC:
            self.seen.append(
                (str(payload["status"]), payload.get("worker_pgid") is not None)
            )


@pytest.fixture
def log() -> Iterator[_PublishLog]:
    recorder = _PublishLog()
    set_hub(recorder)
    yield recorder
    set_hub(None)


@pytest.fixture
def kinds() -> Iterator[None]:
    register_worker("mute", lambda _p: [sys.executable, "-c", _MUTE])
    register_worker("brief", lambda _p: [sys.executable, "-c", _BRIEF])
    yield
    unregister_worker("mute")
    unregister_worker("brief")


def _store(tmp_path: Path) -> JobStore:
    store = JobStore(tmp_path / "jobs.db", boot_id="boot-a", owner_pid=os.getpid())
    store.recover()
    return store


def test_the_pgid_is_written_before_the_runner_yields_after_the_fork(
    tmp_path: Path, kinds: None, log: _PublishLog
) -> None:
    """The first thing published after 'running' must carry the pgid.

    The worker never writes a line, so nothing else can publish in between.
    Anything appearing before the pgid write would be an await that had been
    inserted into the window, which is the regression this pins.
    """
    store = _store(tmp_path)

    async def drive() -> None:
        runner = JobRunner(store, poll_s=0.01)
        await runner.start()
        job = store.enqueue("mute", {})
        deadline = asyncio.get_running_loop().time() + 20
        while store.get(job["id"])["worker_pgid"] is None:
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("the worker pgid was never recorded")
            await asyncio.sleep(0.01)
        await runner.stop()

    asyncio.run(drive())

    after_claim = log.seen[log.seen.index(("running", False)) + 1 :]
    assert after_claim, "nothing was published after the claim"
    assert after_claim[0] == ("running", True), (
        f"something was published between the fork and the pgid write: "
        f"{after_claim[:3]}"
    )
    store.close()


def test_shutdown_covers_a_job_still_inside_its_spawn_window(
    tmp_path: Path, kinds: None
) -> None:
    """stop() must cancel what was CLAIMED, not only what has registered.

    The job below is spawned and then shut down before its task has run a
    single line, which is the window at its widest. stop() iterated
    ``_running`` -- empty at that instant -- so nothing cancelled it, and the
    final gather then waited for the worker to finish on its own. Under load
    the full suite hit this for real and spent ten minutes inside one
    shutdown, with the worker outliving the engine that was supposed to own
    it.
    """
    store = _store(tmp_path)

    async def drive() -> dict[str, Any]:
        runner = JobRunner(store, poll_s=10.0)
        job = store.enqueue("brief", {})
        (claimed,) = store.claim_queued(limit=1)
        runner._spawn(claimed)
        assert not runner._running, "the premise is wrong: it already forked"
        await asyncio.wait_for(runner.stop(), timeout=_SHUTDOWN_BUDGET_S)
        return store.get(job["id"])

    final = asyncio.run(drive())

    assert final["status"] == "cancelled", final
    assert final["worker_pgid"] is not None, (
        "the row must still name the group that was killed"
    )
    store.close()


def test_a_row_with_no_recorded_pgid_names_the_spawn_window(
    tmp_path: Path,
) -> None:
    """Recovery must not call an unidentifiable worker 'nothing to reap'.

    The row is claimed and then abandoned by a dying engine before any pgid
    was written -- indistinguishable, from the next boot's point of view, from
    a row that never spawned at all. That ambiguity IS the finding, so the
    error has to carry it instead of picking the reassuring half.
    """
    boot_a = _store(tmp_path)
    job = boot_a.enqueue("never-recorded", {})
    boot_a.claim_queued()
    boot_a.close()

    boot_b = JobStore(
        tmp_path / "jobs.db", boot_id="boot-b", owner_pid=os.getpid()
    )
    recovered = boot_b.recover()

    assert [row["id"] for row in recovered] == [job["id"]]
    error = recovered[0]["error"]
    assert "no worker pgid was ever recorded" in error, error
    assert "between the fork and the write" in error, error
    assert "nothing that can now identify it" in error, error
    assert "nothing to reap" not in error, (
        f"the benign sentence is still there: {error}"
    )
    boot_b.close()


def test_cancelling_an_unheld_running_row_names_the_spawn_window(
    tmp_path: Path, kinds: None
) -> None:
    """The runner's own orphan message must name the condition too.

    A 'running' row this engine holds no worker for got in one of two ways:
    the supervisor died in the spawn window, or the row belongs to a boot that
    is gone. Either way a process may still be running that nothing can name,
    which is what 'unknown' has to mean here.
    """
    store = _store(tmp_path)
    job = store.enqueue("mute", {})
    store.claim_queued()

    async def drive() -> dict[str, Any]:
        runner = JobRunner(store, poll_s=0.01)
        return await runner.cancel(job["id"])

    final = asyncio.run(drive())

    assert final["status"] == "unknown", final
    error = final["error"]
    assert "spawn window" in error, error
    assert "worker_pgid" in error, error
    assert "cannot be established from here" in error, error
    store.close()



def test_a_fork_outlasting_the_settle_window_neither_holds_stop_nor_escapes(
    tmp_path: Path, kinds: None
) -> None:
    """stop() returns at the settle deadline, and the late worker still dies.

    With a zero settle window the job below is still forking when stop()
    gives up on it, so stop() must return without waiting for it: waiting
    there was unbounded, past the desktop shell's grace. Its real fork then
    completes on the same loop AFTER stop() has walked the workers, so
    nothing in stop() can cancel it. The worker must cancel itself on
    registering, or its separate session outlives the engine.
    """
    store = _store(tmp_path)

    async def drive() -> tuple[bool, dict[str, Any]]:
        runner = JobRunner(store, poll_s=10.0, spawn_settle_s=0.0)
        job = store.enqueue("mute", {})
        (claimed,) = store.claim_queued(limit=1)
        runner._spawn(claimed)
        assert not runner._running, "the premise is wrong: it already forked"
        task = runner._tasks[job["id"]]
        await asyncio.wait_for(runner.stop(), timeout=_SHUTDOWN_BUDGET_S)
        returned_before_fork = not task.done()
        await asyncio.wait_for(task, timeout=_SHUTDOWN_BUDGET_S)
        return returned_before_fork, store.get(job["id"])

    returned_before_fork, final = asyncio.run(drive())

    assert returned_before_fork, "stop() waited for a fork past its settle window"
    assert final["status"] == "cancelled", final
    assert final["worker_pgid"] is not None, "the late worker never registered"
    store.close()
