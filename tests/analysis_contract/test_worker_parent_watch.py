"""Analysis pool workers exit when their parent is gone (quit hang, Fri 2 Oct 2026).

[if] a pool worker outlives its parent [then] it blocks reinstalls forever, [else stop].

A spawned pool worker blocks on a call queue that never reports end-of-file
when the parent dies, so three of them outlived the app on demon-llama and
blocked the DMG installer. These cases build the production pool
(``spawn_pool``) in a real owner process and kill the owner.

Regression lines:
  - if a worker whose parent died keeps running then the orphan stays -> broken
  - if a worker exits while its parent is alive then analysis dies for
    nothing -> broken (the overshoot)
  - if the watch trusts the pid alone then a Windows orphan (never
    reparented) or a reused pid reads as a live parent -> broken
  - if a worker reads its parent's start time itself instead of taking the
    owner's record then an owner that died and had its pid recycled before
    the worker started reads as alive forever -> broken
"""
from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from apps.analysis import worker_diagnostics

pytestmark = pytest.mark.requirement("INSTALL-33")

# The pool owner: build the production analysis pool (``spawn_pool``: spawn
# context, ``init_worker`` with the owner's identity), keep every worker busy,
# report their pids, then exit at once (argv[1] == 'die') or stay alive.
_OWNER = (
    "import os, sys, time\n"
    "from apps.analysis.pool import spawn_pool\n"
    "pool = spawn_pool(2)\n"
    "futures = [pool.submit(time.sleep, 60) for _ in range(2)]\n"
    "while len(pool._processes) < 2:\n"
    "    time.sleep(0.05)\n"
    "print(' '.join(str(pid) for pid in pool._processes), flush=True)\n"
    "if sys.argv[1] == 'die':\n"
    "    os._exit(0)\n"
    "time.sleep(60)\n"
)

# A bare worker that arms the watch with the start time it is handed, for the
# one case a real pool cannot stage: an owner whose pid was recycled before
# the worker started.
_WORKER = (
    "import sys, time\n"
    "from apps.analysis.worker_diagnostics import watch_parent\n"
    "watch_parent(int(sys.argv[1]), float(sys.argv[2]), poll_s=0.05)\n"
    "time.sleep(60)\n"
)


def _start(mode: str) -> tuple[subprocess.Popen[str], list[int]]:
    owner = subprocess.Popen(
        [sys.executable, "-c", _OWNER, mode],
        cwd=Path(__file__).resolve().parents[2],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert owner.stdout is not None
    return owner, [int(pid) for pid in owner.stdout.readline().split()]


def _wait_gone(pid: int, within_s: float) -> bool:
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            return True
        time.sleep(0.05)
    return False


def _cleanup(owner: subprocess.Popen[str], workers: list[int]) -> None:
    for pid in (*workers, owner.pid):
        with contextlib.suppress(psutil.NoSuchProcess):
            psutil.Process(pid).kill()
    owner.wait()


def test_pool_workers_exit_once_their_owner_is_gone() -> None:
    owner, workers = _start("die")
    try:
        assert len(workers) == 2, workers
        owner.wait(timeout=10)
        for pid in workers:
            assert _wait_gone(pid, within_s=15), f"orphaned pool worker {pid} kept running"
    finally:
        _cleanup(owner, workers)


def test_pool_workers_keep_running_while_their_owner_lives() -> None:
    owner, workers = _start("live")
    try:
        assert len(workers) == 2, workers
        # Several poll intervals: a watch that fires on a live owner fires here.
        time.sleep(3 * worker_diagnostics.PARENT_POLL_S)
        for pid in workers:
            assert psutil.Process(pid).status() != psutil.STATUS_ZOMBIE, f"worker {pid} exited"
    finally:
        _cleanup(owner, workers)


def test_parent_gone_compares_against_the_recorded_parent() -> None:
    started_at = worker_diagnostics.parent_started_at(os.getpid())
    assert started_at is not None
    assert worker_diagnostics.parent_gone(os.getpid(), started_at) is False
    # Same pid, another start time: the pid was reused, which is what an
    # unreparented Windows orphan's pid looks like once the OS recycles it.
    assert worker_diagnostics.parent_gone(os.getpid(), started_at - 1.0) is True


def test_a_dead_parent_is_gone_by_pid_and_start_time() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        started_at = worker_diagnostics.parent_started_at(proc.pid)
        assert started_at is not None
        assert worker_diagnostics.parent_gone(proc.pid, started_at) is False
    finally:
        proc.kill()
        proc.wait()
    assert worker_diagnostics.parent_gone(proc.pid, started_at) is True


def test_a_worker_trusts_the_owners_record_over_whoever_holds_the_pid() -> None:
    # The owner recorded a start time; the process holding its pid now has
    # another one, which is what a pid recycled before the worker started
    # looks like. The worker must leave even though that pid is alive.
    holder = os.getpid()
    recorded = worker_diagnostics.parent_started_at(holder)
    assert recorded is not None
    worker = subprocess.Popen([sys.executable, "-c", _WORKER, str(holder), str(recorded - 1.0)])
    try:
        assert worker.wait(timeout=10) == worker_diagnostics.EXIT_PARENT_GONE
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait()


def test_the_owner_hands_workers_its_own_identity() -> None:
    pid, started_at = worker_diagnostics.owner_identity()
    assert pid == os.getpid()
    assert started_at == worker_diagnostics.parent_started_at(os.getpid())
