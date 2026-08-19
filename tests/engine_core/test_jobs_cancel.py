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

A QUEUED row is the other half of the same story (C14). It has no worker, so
there is nothing to prove dead and the transition is legal in one step -- but
only if the supervisor can never claim it afterwards, which is what makes the
claim a compare-and-swap rather than a blind write.

Single-line intent:
  - if cancel skips 'cancelling' then there is no state that means "asked to
    stop but not yet proven stopped", and a wedged worker reads as stopped
  - if 'cancelled' is written while the group is still alive then the status
    is a claim about the world that is not true
  - if the whole TREE is not taken then cancelling a worker with children
    leaves the children running
  - if a queued job cannot be cancelled then the only way to stop it is to let
    it start first, which is the opposite of what the caller asked for
  - if the claim is not conditional on the row still being queued then a
    cancelled job is resurrected into 'running' and gets a worker anyway
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

# Writes a file the moment it is executed. If this ever runs, the queued job
# it belongs to was spawned, which is the exact thing a queued cancel forbids.
_TATTLE = "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text('spawned')"

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


class _FetchedRows:
    """A cursor stand-in holding rows that were read BEFORE the interference."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def fetchall(self) -> list[Any]:
        return self._rows


class _InterleavingConn:
    """The store's connection, with one cancel wedged into the claim.

    sqlite3.Connection.execute is read-only, so the stale read is forced by
    standing in front of the whole connection instead. Everything is delegated
    untouched except the claim's SELECT, whose rows are drained FIRST and only
    then followed by a status flip on the real connection. Draining first is
    the point: interfering while the scan is still stepping merely makes the
    SELECT skip the row, which proves nothing about the UPDATE. What has to be
    reproduced is a batch the claim has already committed to, whose contents
    stopped being true before the swap ran.
    """

    def __init__(self, conn: Any, cancel_id: str) -> None:
        self._conn = conn
        self._cancel_id = cancel_id
        self.fired = False

    def execute(self, sql: str, params: Any = ()) -> Any:
        cursor = self._conn.execute(sql, params)
        if not sql.startswith("SELECT id FROM jobs") or self.fired:
            return cursor
        self.fired = True
        rows = cursor.fetchall()
        self._conn.execute(
            "UPDATE jobs SET status='cancelled' WHERE id=?", (self._cancel_id,)
        )
        return _FetchedRows(rows)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


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
    register_worker(
        "cancel-tattle",
        lambda p: [sys.executable, "-c", _TATTLE, str(p["marker"])],
    )
    yield
    unregister_worker("cancel-sleeper")
    unregister_worker("cancel-tree")
    unregister_worker("cancel-tattle")


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


def test_a_queued_job_cancels_without_ever_spawning_a_worker(
    tmp_path: Path, kinds: None, witness: _StatusWitness
) -> None:
    """queued -> cancelled is legal, and it must beat the supervisor to it.

    Before C14 this 409'd: begin_cancel only accepted 'running', so the only
    way to stop a queued job was to wait for it to start. The worker here
    writes a file the instant it executes, so "never spawned" is proven by the
    filesystem rather than by the status the store happens to report.
    """
    store = _store(tmp_path)
    marker = tmp_path / "spawned.txt"

    async def drive() -> dict[str, Any]:
        runner = JobRunner(store, poll_s=0.01)
        job = store.enqueue("cancel-tattle", {"marker": str(marker)})
        final = await runner.cancel(job["id"])
        # Only NOW is the supervisor allowed to look at the queue. A cancelled
        # row it can still claim is the same bug wearing a different hat.
        await runner.start()
        await asyncio.sleep(0.2)
        await runner.stop()
        return store.get(final["id"])

    final = asyncio.run(drive())

    assert final["status"] == "cancelled", final
    assert final["worker_pid"] is None, final
    assert final["worker_pgid"] is None, final
    assert final["started_at"] is None, "a cancelled queued row never started"
    assert final["finished_at"] is not None, final
    assert not marker.exists(), (
        "the worker ran anyway; the cancel did not beat the claim"
    )
    # No 'cancelling' phase: there is no group whose death has to be proven.
    assert witness.statuses() == ["queued", "cancelled"], witness.statuses()
    store.close()


def test_a_row_cancelled_after_the_claim_read_it_is_not_claimed(
    tmp_path: Path,
) -> None:
    """The claim is a compare-and-swap, not a blind write.

    The SELECT and the UPDATE share one BEGIN IMMEDIATE today, so this
    interleave cannot arise from inside the process -- which is exactly why it
    has to be pinned rather than assumed. The guard is what makes the ordering
    safe: without ``AND status='queued'`` a claim that read a queued row
    resurrects it into 'running' whatever it became in between, and a
    cancelled job gets a worker.

    The interleave is forced by mutating the row on the store's own connection
    between the SELECT and the UPDATE, inside the claim's open transaction.
    That is the stale read, reproduced exactly.
    """
    store = _store(tmp_path)
    survivor = store.enqueue("cancel-sleeper", {})
    doomed = store.enqueue("cancel-sleeper", {})
    real_conn = store._conn
    interleaving = _InterleavingConn(real_conn, doomed["id"])

    store._conn = interleaving  # type: ignore[assignment]
    try:
        claimed = store.claim_queued(limit=2)
    finally:
        store._conn = real_conn

    assert interleaving.fired, "the interleave never fired; this proves nothing"
    assert [row["id"] for row in claimed] == [survivor["id"]], claimed
    assert store.get(doomed["id"])["status"] == "cancelled", (
        "the claim overwrote a cancelled row and would have spawned a worker"
    )
    assert store.get(survivor["id"])["status"] == "running"
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
