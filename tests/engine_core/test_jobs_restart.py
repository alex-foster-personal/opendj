"""Restart survival: what a killed engine leaves behind, and what boot 2 does.

The load-bearing claims:

  * a job left ``running`` under a foreign boot_id becomes ``unknown``, never
    ``failed`` -- the engine does not know whether the side effect landed;
  * the orphaned worker's process GROUP is actually killed, with its identity
    proven first;
  * a pgid whose identity does NOT check out is left strictly alone;
  * an ``unknown`` row refuses to re-enqueue until a reconcile hook can say
    what really happened.

A real subprocess is used throughout. Reaping a process that does not exist
would prove nothing.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from apps.engine_core.jobs.reap import (
    WorkerIdentity,
    process_group_exists,
)
from apps.engine_core.jobs.runner import (
    register_reconcile,
    resolve_and_reenqueue,
    unregister_worker,
)
from apps.engine_core.jobs.store import RESTART_ERROR, JobConflict, JobStore

REPO_ROOT = Path(__file__).resolve().parents[2]

# A whole engine, in its own process, holding one live worker. The test
# SIGKILLs THIS, which is the only way to produce a genuinely orphaned worker
# rather than a tidy simulation of one. It prints the running row and then
# blocks forever waiting to be killed.
_ENGINE_SOURCE = """
import asyncio, json, os, sys
from apps.engine_core.jobs.runner import JobRunner, register_worker
from apps.engine_core.jobs.store import JobStore

SLEEPER = (
    "import json, time\\n"
    "print(json.dumps({'progress': 0.1, 'message': 'started'}), flush=True)\\n"
    "time.sleep(600)\\n"
)
register_worker("test-sleeper", lambda _p: [sys.executable, "-c", SLEEPER])
store = JobStore(sys.argv[1], boot_id="boot-a", owner_pid=os.getpid())
store.recover()

async def main():
    runner = JobRunner(store)
    await runner.start()
    job = store.enqueue("test-sleeper", {})
    while True:
        row = store.get(job["id"])
        if row["status"] == "running" and row["worker_pgid"] is not None:
            print(json.dumps(row), flush=True)
            break
        await asyncio.sleep(0.05)
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


def _start_engine(db_path: Path) -> tuple[subprocess.Popen[str], dict]:
    """Boot a real engine process and wait for it to be holding a worker."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    engine = subprocess.Popen(
        [sys.executable, "-c", _ENGINE_SOURCE, str(db_path)],
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
        raise AssertionError(
            f"engine never reported a running worker: {engine.stderr.read()}"
        )
    return engine, json.loads(line)


def test_restart_flips_running_to_unknown_and_reaps_the_worker(
    tmp_path: Path,
) -> None:
    """SIGKILL a real engine mid-job; boot 2 must clean up after it.

    Nothing here is simulated: a separate process really is holding a really
    running worker when it really is killed without warning.
    """
    db_path = tmp_path / "jobs.db"
    engine, running = _start_engine(db_path)
    pgid = running["worker_pgid"]
    engine.kill()  # SIGKILL: no shutdown hook, no cancellation, no mercy
    engine.wait(timeout=10)

    try:
        assert running["status"] == "running"
        assert process_group_exists(pgid), "orphan died before the test ran"

        boot_b = _store(db_path, "boot-b")
        recovered = boot_b.recover()

        assert len(recovered) == 1
        row = recovered[0]
        assert row["status"] == "unknown", row
        assert RESTART_ERROR in row["error"]
        assert "reap:" in row["error"], row["error"]
        assert not process_group_exists(pgid), row["error"]

        # A second recovery pass has nothing left to do: the row is now owned
        # by boot-b and terminal.
        assert boot_b.recover() == []
        boot_b.close()
    finally:
        _kill_group(pgid)


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
