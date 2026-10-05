"""Restart survival: what a killed engine leaves behind, and what boot 2 does.

The load-bearing claims:

  * a job left ``running`` under a foreign boot_id becomes ``unknown``, never
    ``failed`` -- the engine does not know whether the side effect landed;
  * the orphaned worker's process GROUP is actually killed, with its identity
    proven first;
  * a pgid whose identity does NOT check out is left strictly alone;
  * a pgid whose process is already GONE is reported as a skip WITH the
    reason, never quietly passed over;
  * an ``unknown`` row refuses to re-enqueue until a reconcile hook can say
    what really happened.

A real subprocess is used throughout. Reaping a process that does not exist
would prove nothing.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import psutil
import pytest

from apps.engine_core.jobs.reap import (
    WorkerIdentity,
    group_has_live_member,
    process_group_exists,
)
from apps.engine_core.jobs.runner import (
    register_reconcile,
    resolve_and_reenqueue,
    unregister_worker,
)
from apps.engine_core.jobs.store import RESTART_ERROR, JobConflict, JobStore

REPO_ROOT = Path(__file__).resolve().parents[2]

#: How long the orphaned worker may take to become reapable after the engine's
#: SIGKILL before the test reports UNKNOWN rather than red. Normally the group
#: is there the instant the engine dies; the bound exists so a host that cannot
#: satisfy the premise names it as UNKNOWN instead of failing recovery code for
#: the scheduler's own lateness (issue #1156). The UNKNOWN skip, not this
#: number, is the backstop.
ORPHAN_GRACE_S: float = 10.0


def _poll_until(
    predicate: Callable[[], bool], *, deadline_s: float, poll_s: float = 0.05
) -> bool:
    """First True from `predicate` before `deadline_s`; False once it elapses.

    The bounded poll for this module's liveness control. A control that checks
    once measures the scheduler, not the subject; polling to a deadline turns a
    slow start into a late start, and a fixed `time.sleep(N)` would be the same
    defect one constant later. The caller decides what an expired deadline
    means - here, an UNKNOWN skip, never a red.
    """
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll_s)
    return predicate()

# A whole engine, in its own process, holding one live worker. The test
# SIGKILLs THIS, which is the only way to produce a genuinely orphaned worker
# rather than a tidy simulation of one. It prints the running row and then
# blocks forever waiting to be killed.
_ENGINE_SOURCE = """
import asyncio, json, os, sys, time
from apps.engine_core.jobs.runner import JobRunner, register_worker
from apps.engine_core.jobs.store import JobStore

# argv[1] is the jobs db; argv[2] is the sentinel the WORKER touches once it
# is past its only write to the stdout pipe it shares with this engine. See
# the test's _await_worker_ready for why that ordering is the whole
# precondition of the reap test.
SLEEPER = (
    "import json, sys, time\\n"
    "print(json.dumps({'progress': 0.1, 'message': 'started'}), flush=True)\\n"
    "open(sys.argv[1], 'w').close()\\n"
    "time.sleep(600)\\n"
)
register_worker(
    "test-sleeper", lambda _p: [sys.executable, "-c", SLEEPER, sys.argv[2]]
)
store = JobStore(sys.argv[1], boot_id="boot-a", owner_pid=os.getpid())
store.recover()

async def main():
    runner = JobRunner(store)
    await runner.start()
    job = store.enqueue("test-sleeper", {})
    # Readiness means the worker has run far enough to emit its FIRST progress
    # line, not merely that a pid was recorded. The worker writes to a pipe
    # whose read end this engine holds; if the test SIGKILLs the engine while
    # the worker is still between fork and that first write, the write lands on
    # a closed pipe and the worker dies with it (BrokenPipeError). On a loaded
    # box the pid is recorded before the worker's interpreter has started, so
    # announcing on the pid alone is exactly the "orphan died before the test
    # ran" race of issue #1156. A worker whose progress line was READ is past
    # its only write and cannot die with the engine, so the orphan genuinely
    # survives the SIGKILL.
    deadline = time.time() + 30
    while time.time() < deadline:
        row = store.get(job["id"])
        if (
            row["status"] == "running"
            and row["worker_pgid"] is not None
            and (row["progress"] or 0) > 0
        ):
            print(json.dumps(row), flush=True)
            break
        await asyncio.sleep(0.05)
    else:
        print("engine: worker never emitted its first progress line", file=sys.stderr)
        sys.exit(3)
    await asyncio.sleep(600)

asyncio.run(main())
"""


def _store(db_path: Path, boot_id: str) -> JobStore:
    return JobStore(db_path, boot_id=boot_id, owner_pid=os.getpid())


def _kill_group(pgid: int | None) -> None:
    if pgid is None:
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return


def _spawn_orphan() -> subprocess.Popen[bytes]:
    """A real, live process group with no engine attached to it."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _start_engine(
    db_path: Path, ready: Path
) -> tuple[subprocess.Popen[str], dict]:
    """Boot a real engine process and wait for it to be holding a worker."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    engine = subprocess.Popen(
        [sys.executable, "-c", _ENGINE_SOURCE, str(db_path), str(ready)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
    )
    assert engine.stdout is not None
    line = engine.stdout.readline()
    if not line:
        engine.kill()
        assert engine.stderr is not None
        raise AssertionError(
            f"engine never reported a running worker: {engine.stderr.read()}"
        )
    return engine, json.loads(line)


def _await_worker_ready(
    engine: subprocess.Popen[str], ready: Path, timeout_s: float = 60.0
) -> None:
    """Block until the worker is past the one write the engine's death breaks.

    THE PRECONDITION THIS EXISTS TO MAKE TRUE. ``worker_pgid`` is written the
    statement after the fork, so the engine reports the job as running while
    the worker is still inside interpreter startup. SIGKILL the engine in that
    window and the worker's first ``print`` lands on a pipe whose only reader
    just died: BrokenPipeError, and the worker goes down with it. The next
    boot then finds no orphan at all and correctly says "reap skipped: pid N
    is not running" -- a red test with a healthy engine behind it, and the
    likelihood of landing in that window rises with how loaded the machine is
    (measured on darwin: an orphan whose print was held back died at exactly
    the moment it reached that print, not a millisecond before).

    So the worker announces itself on DISK instead, AFTER that print. The
    sentinel existing proves the write already happened; from there the worker
    only sleeps, touching nothing the engine's death can break.
    """
    deadline = time.monotonic() + timeout_s
    while not ready.exists():
        if engine.poll() is not None:
            assert engine.stderr is not None
            raise AssertionError(
                "engine died before its worker was ready: "
                f"{engine.stderr.read()}"
            )
        if time.monotonic() >= deadline:
            engine.kill()
            raise AssertionError(
                f"worker never wrote its ready sentinel {ready} within "
                f"{timeout_s:.0f}s, so there is no live orphan to reap"
            )
        time.sleep(0.02)


def _why_not_live(pgid: int) -> str | None:
    """Why nothing is RUNNING under ``pgid``, or None when something is.

    ``process_group_exists`` is the wrong probe for a precondition: a pgid
    stays allocated while its last member is a zombie, so it answers yes for a
    group that is already a corpse. This one discounts zombies and names the
    reason, so a precondition that does not hold fails loudly with the fact
    rather than passing quietly.
    """
    if not group_has_live_member(pgid):
        return f"nothing is still running in process group {pgid}"
    try:
        status = psutil.Process(pgid).status()
    except psutil.NoSuchProcess:
        return f"pid {pgid} is gone"
    if status == psutil.STATUS_ZOMBIE:
        return f"pid {pgid} is a zombie"
    return None


def test_restart_flips_running_to_unknown_and_reaps_the_worker(
    tmp_path: Path,
) -> None:
    """SIGKILL a real engine mid-job; boot 2 must clean up after it.

    Nothing here is simulated: a separate process really is holding a really
    running worker when it really is killed without warning. The worker is
    held to its ready sentinel first, because an orphan that has not survived
    the engine's death yet is not an orphan -- see _await_worker_ready.
    """
    db_path = tmp_path / "jobs.db"
    ready = tmp_path / "worker-ready"
    engine, running = _start_engine(db_path, ready)
    pgid = running["worker_pgid"]
    _await_worker_ready(engine, ready)
    engine.kill()  # SIGKILL: no shutdown hook, no cancellation, no mercy
    engine.wait(timeout=10)

    try:
        assert running["status"] == "running"
        # Bounded poll, not a single check: on a loaded box the kernel may
        # not have settled the group at the instant we look, and a control that
        # probes once measures the scheduler rather than the subject. If the
        # premise never holds, the honest verdict is UNKNOWN, not a red blaming
        # recovery (issue #1156).
        #
        # The predicate is _why_not_live, NOT process_group_exists: a pgid stays
        # allocated while its last member is a zombie, so process_group_exists
        # answers yes for a group that is already a corpse, and polling it would
        # establish the premise against a dead orphan.
        if not _poll_until(
            lambda: _why_not_live(pgid) is None, deadline_s=ORPHAN_GRACE_S
        ):
            pytest.skip(
                f"UNKNOWN, not a pass: orphaned worker group {pgid} never had a "
                f"live member within {ORPHAN_GRACE_S:.0f}s of the engine's "
                f"SIGKILL ({_why_not_live(pgid)}), so recovery had no live "
                "orphan to reap and the kill-ladder is not demonstrable this run"
            )

        boot_b = _store(db_path, "boot-b")
        recovered = boot_b.recover()

        assert len(recovered) == 1
        row = recovered[0]
        assert row["status"] == "unknown", row
        assert RESTART_ERROR in row["error"]
        assert "reap:" in row["error"], row["error"]
        # Live members, not the pgid: the group stays allocated while the
        # corpse waits to be reaped by whatever inherited it, and "still
        # allocated" is not "still running". Bounded rather than instant,
        # because on a loaded box the reap can still be in flight when recovery
        # returns (issue #1156); the assertion below is what goes red if the
        # reap genuinely lied.
        _poll_until(
            lambda: not group_has_live_member(pgid), deadline_s=ORPHAN_GRACE_S
        )
        assert not group_has_live_member(pgid), row["error"]

        # A second recovery pass has nothing left to do: the row is now owned
        # by boot-b and terminal.
        assert boot_b.recover() == []
        boot_b.close()
    finally:
        _kill_group(pgid)


def test_liveness_poll_waits_for_a_process_group_that_appears_late() -> None:
    """if the bounded wait collapses to a single probe then a slow start reddens.

    The regression for `_poll_until` with a REAL late subject: the child calls
    `setsid` only after a delay, so for that delay the pid does not exist as a
    process group (the child is still in ours) and a single
    `process_group_exists` reads False. The poll must wait for the setsid to
    land. This fails if the wait is removed - the exact shape that reddened
    issue #1156.
    """
    child = os.fork()
    if child == 0:
        time.sleep(1.0)
        os.setsid()
        time.sleep(600)
        os._exit(0)  # pragma: no cover - only the parent reaches here
    try:
        assert _poll_until(
            lambda: process_group_exists(child), deadline_s=ORPHAN_GRACE_S
        ), "the poll never saw the late process group"
    finally:
        # Signal the child BY PID, not by group: until the child calls setsid
        # it is still in OUR group and killpg(child) is ESRCH, which would
        # strand it in its 600s sleep and hang the waitpid below - exactly the
        # slow failure this module must not hand back (a mutation run caught
        # it: 601s). The pid is always killable while the child exists.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(child, signal.SIGKILL)
        os.waitpid(child, 0)


def test_recovery_refuses_to_kill_a_pgid_it_cannot_prove(
    tmp_path: Path,
) -> None:
    """A recycled pid must survive recovery untouched.

    The row points at a REAL live process whose argv does not match what was
    recorded. Blind-killpg would take out a stranger; this asserts it does
    not happen.
    """
    db_path = tmp_path / "jobs.db"
    boot_a = _store(db_path, "boot-a")
    boot_a.recover()
    job = boot_a.enqueue("stale-kind", {})
    boot_a.claim_queued()

    stranger = _spawn_orphan()
    try:
        identity = WorkerIdentity(
            pgid=stranger.pid,
            argv=("/definitely/not/what/is/running",),
            started_at=time.time(),
        )
        boot_a.record_worker(job["id"], pid=stranger.pid, identity=identity)
        boot_a.close()

        boot_b = _store(db_path, "boot-b")
        recovered = boot_b.recover()
        boot_b.close()

        assert len(recovered) == 1
        assert recovered[0]["status"] == "unknown"
        assert "reap skipped" in recovered[0]["error"], recovered[0]["error"]
        assert stranger.poll() is None, "recovery killed an unverified process"
    finally:
        _kill_group(stranger.pid)
        stranger.wait(timeout=10)


def test_recovery_reports_a_dead_pgid_as_skipped_with_the_reason(
    tmp_path: Path,
) -> None:
    """The other half of the invariant: a pid that is GONE is said so, loudly.

    A running orphan MUST be reaped. An orphan that died on its own MUST be
    reported as a skip WITH the reason, never passed over in silence -- the
    row is the only place that distinction can be recorded. This branch used
    to be reached only by accident, when the reap test's own worker died in
    the spawn window; it is worth reaching on purpose.
    """
    db_path = tmp_path / "jobs.db"
    boot_a = _store(db_path, "boot-a")
    boot_a.recover()
    job = boot_a.enqueue("dead-worker", {})
    boot_a.claim_queued()

    corpse = _spawn_orphan()
    boot_a.record_worker(
        job["id"],
        pid=corpse.pid,
        identity=WorkerIdentity(
            pgid=corpse.pid,
            argv=("python", "-c", "sleep"),
            started_at=time.time(),
        ),
    )
    boot_a.close()
    corpse.kill()
    # Waited on here, so the pid is RELEASED rather than left a zombie: the
    # branch under test is a leader psutil cannot find at all.
    corpse.wait(timeout=10)
    assert not process_group_exists(corpse.pid), "the corpse outlived its kill"

    boot_b = _store(db_path, "boot-b")
    try:
        recovered = boot_b.recover()
        assert len(recovered) == 1
        row = recovered[0]
        assert row["status"] == "unknown", row
        assert RESTART_ERROR in row["error"]
        assert f"reap skipped: pid {corpse.pid} is not running" in row["error"]
        # Nothing of ours can still be in that group, so the row is done with
        # and the next boot has no reap left to retry.
        assert boot_b.recover() == []
    finally:
        boot_b.close()


def test_unknown_rows_refuse_to_reenqueue_until_reconciled(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "jobs.db"
    boot_a = _store(db_path, "boot-a")
    boot_a.recover()
    job = boot_a.enqueue("needs-reconcile", {})
    boot_a.claim_queued()
    boot_a.close()

    boot_b = _store(db_path, "boot-b")
    try:
        boot_b.recover()
        assert boot_b.get(job["id"])["status"] == "unknown"

        with pytest.raises(JobConflict) as refusal:
            resolve_and_reenqueue(boot_b, job["id"])
        assert "unknown" in str(refusal.value)

        register_reconcile("needs-reconcile", lambda _row: "failed")
        try:
            requeued = resolve_and_reenqueue(boot_b, job["id"])
        finally:
            unregister_worker("needs-reconcile")
        assert requeued["status"] == "queued"
        assert requeued["attempt"] == 1
        assert requeued["worker_pgid"] is None
    finally:
        boot_b.close()


def test_recovery_leaves_this_boots_own_rows_alone(tmp_path: Path) -> None:
    """Recovery keys on owner_boot_id, not on 'is anything running'."""
    db_path = tmp_path / "jobs.db"
    store = _store(db_path, "boot-a")
    store.recover()
    job = store.enqueue("mine", {})
    store.claim_queued()
    assert store.recover() == []
    assert store.get(job["id"])["status"] == "running"
    instance = store.instance()
    assert instance is not None
    assert instance["boot_id"] == "boot-a"
    store.close()


def test_worker_identity_columns_survive_a_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.db"
    store = _store(db_path, "boot-a")
    store.recover()
    job = store.enqueue("mine", {"a": 1})
    store.claim_queued()
    identity = WorkerIdentity(pgid=4242, argv=("a", "b"), started_at=1.5)
    store.record_worker(job["id"], pid=4242, identity=identity)
    store.close()

    reopened = _store(db_path, "boot-a")
    row = reopened.get(job["id"])
    reopened.close()
    assert row["worker_pgid"] == 4242
    assert row["worker_argv"] == ["a", "b"]
    assert row["worker_started_at"] == 1.5
    assert row["payload"] == {"a": 1}
    assert json.loads(json.dumps(row)) == row, "row must be JSON publishable"
