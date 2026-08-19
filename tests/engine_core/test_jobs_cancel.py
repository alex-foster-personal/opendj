"""Cancel is two-phase, and the second phase is earned.

runner.cancel documents "running -> cancelling -> (group proven dead) ->
cancelled", and store.begin_cancel explains why: "the row is only 'cancelled'
once the group is confirmed dead, so a stuck worker never reads as tidily
stopped". Nothing tested it, so 'cancelled' could have been a status poke with
a live process behind it and the suite would have stayed green.

The witness below hooks the events seam -- the same set_hub() the chassis uses
-- and records process-group liveness AT THE INSTANT each status is written.
That is what turns "the final row says cancelled" into "the group was already
dead when it said so".

Single-line intent:
  - if cancel skips 'cancelling' then there is no state that means "asked to
    stop but not yet proven stopped", and a wedged worker reads as stopped
  - if 'cancelled' is written while the group is still alive then the status
    is a claim about the world that is not true
  - if the whole TREE is not taken then cancelling a worker with children
    leaves the children running
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psutil
import pytest

from apps.engine_core.jobs.reap import group_has_live_member
from apps.engine_core.jobs.runner import (
    JobRunner,
    register_worker,
    unregister_worker,
)
from apps.engine_core.jobs.store import JOBS_TOPIC, JobStore
from apps.shared.events import set_hub

_SLEEPER = "import time; time.sleep(600)"

# Spawns a child WITHOUT start_new_session, so the child stays in the worker's
# process group and a cancel has a real tree to take, not a lone process.
_PARENT_OF_CHILD = (
    "import json, subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
    "print(json.dumps({'progress': 0.1, 'message': str(child.pid)}), flush=True)\n"
    "time.sleep(600)\n"
)


class _StatusWitness:
    """Records (status, group_was_alive) as each write is published."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, bool | None]] = []

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        if topic != JOBS_TOPIC:
            return
        pgid = payload.get("worker_pgid")
        alive = group_has_live_member(int(pgid)) if pgid else None
        self.seen.append((str(payload["status"]), alive))

    def statuses(self) -> list[str]:
        """Consecutive duplicates collapsed: progress writes republish a row."""
        out: list[str] = []
        for status, _alive in self.seen:
            if not out or out[-1] != status:
                out.append(status)
        return out

    def liveness_at(self, status: str) -> bool | None:
        for seen_status, alive in self.seen:
            if seen_status == status:
                return alive
        raise AssertionError(f"{status!r} was never published: {self.seen}")


@pytest.fixture
def witness() -> Iterator[_StatusWitness]:
    recorder = _StatusWitness()
    set_hub(recorder)
    yield recorder
    set_hub(None)


@pytest.fixture
def kinds() -> Iterator[None]:
    register_worker("cancel-sleeper", lambda _p: [sys.executable, "-c", _SLEEPER])
    register_worker("cancel-tree", lambda _p: [sys.executable, "-c", _PARENT_OF_CHILD])
    yield
    unregister_worker("cancel-sleeper")
    unregister_worker("cancel-tree")


def _store(tmp_path: Path) -> JobStore:
    store = JobStore(tmp_path / "jobs.db", boot_id="boot-a", owner_pid=os.getpid())
    store.recover()
    return store


async def _await(store: JobStore, job_id: str, wanted: set[str]) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + 20
    while True:
        row = store.get(job_id)
        if row["status"] in wanted and row["worker_pgid"] is not None:
            return row
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"job never reached {wanted}: {row}")
        await asyncio.sleep(0.02)


def test_cancel_reaches_cancelled_only_after_the_group_is_dead(
    tmp_path: Path, kinds: None, witness: _StatusWitness
) -> None:
    store = _store(tmp_path)

    async def drive() -> tuple[int, dict[str, Any]]:
        runner = JobRunner(store, poll_s=0.01)
        await runner.start()
        job = store.enqueue("cancel-sleeper", {})
        running = await _await(store, job["id"], {"running"})
        final = await runner.cancel(job["id"])
        await runner.stop()
        return int(running["worker_pgid"]), final

    pgid, final = asyncio.run(drive())

    assert final["status"] == "cancelled", final
    # Phase one exists and comes first.
    assert witness.statuses() == ["queued", "running", "cancelling", "cancelled"], (
        witness.statuses()
    )
    # Phase one is asked-to-stop: the group was STILL ALIVE when it was written,
    # which is what makes it a distinct state rather than decoration.
    assert witness.liveness_at("cancelling") is True, witness.seen
    # Phase two is proven-stopped: the group was ALREADY DEAD when 'cancelled'
    # was written. This is the claim the whole design rests on.
    assert witness.liveness_at("cancelled") is False, witness.seen
    assert not group_has_live_member(pgid)
    assert "process group" in final["error"], final["error"]
    store.close()


def test_cancel_takes_the_whole_tree_not_just_the_worker(
    tmp_path: Path, kinds: None
) -> None:
    """start_new_session makes the worker a group leader so its children go too."""
    store = _store(tmp_path)

    async def drive() -> tuple[int, int, dict[str, Any]]:
        runner = JobRunner(store, poll_s=0.01)
        await runner.start()
        job = store.enqueue("cancel-tree", {})
        # The worker reports its child's pid through the progress protocol.
        deadline = asyncio.get_running_loop().time() + 20
        while True:
            row = store.get(job["id"])
            if row["message"] and row["message"].isdigit():
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError(f"worker never reported a child: {row}")
            await asyncio.sleep(0.02)
        child_pid = int(row["message"])
        pgid = int(row["worker_pgid"])
        assert psutil.pid_exists(child_pid), "the child died before the test ran"
        assert os.getpgid(child_pid) == pgid, "the child is not in the group"

        final = await runner.cancel(job["id"])
        await runner.stop()
        return pgid, child_pid, final

    pgid, child_pid, final = asyncio.run(drive())
    try:
        assert final["status"] == "cancelled", final
        assert not group_has_live_member(pgid), final["error"]
        assert not psutil.pid_exists(child_pid), (
            f"cancel left the worker's child {child_pid} running: {final['error']}"
        )
    finally:
        if psutil.pid_exists(child_pid):
            os.kill(child_pid, 9)
    store.close()
