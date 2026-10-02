"""Analysis pool workers exit when their parent is gone (quit hang, Fri 2 Oct 2026).

A spawned pool worker blocks on a call queue that never reports end-of-file
when the parent dies, so three of them outlived the app on demon-llama and
blocked the DMG installer. These cases run a real parent and a real worker
that arms the same watch ``init_worker`` arms.

Regression lines:
  - if a worker whose parent died keeps running then the orphan stays -> broken
  - if a worker exits while its parent is alive then analysis dies for
    nothing -> broken (the overshoot)
"""
from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time

import psutil
import pytest

from apps.analysis import worker_diagnostics

pytestmark = [pytest.mark.requirement("INSTALL-30"), pytest.mark.skipif(
    sys.platform == "win32", reason="Windows does not reparent; the watch is POSIX-only"
)]

# The worker: arm the watch against the parent pid it was handed (argv[1]),
# the way the pool owner hands its pid to init_worker, then idle.
_WORKER = (
    "import sys, time\n"
    "from apps.analysis.worker_diagnostics import watch_parent\n"
    "watch_parent(int(sys.argv[1]), poll_s=0.05)\n"
    "time.sleep(60)\n"
)

# The parent: start the worker, report its pid, then either exit at once
# (argv[1] == 'die') or stay alive.
_PARENT = (
    "import os, subprocess, sys, time\n"
    "worker = subprocess.Popen([sys.executable, '-c', sys.argv[2], str(os.getpid())])\n"
    "print(worker.pid, flush=True)\n"
    "if sys.argv[1] == 'die':\n"
    "    os._exit(0)\n"
    "time.sleep(60)\n"
)


def _start(mode: str) -> tuple[subprocess.Popen[str], int]:
    parent = subprocess.Popen(
        [sys.executable, "-c", _PARENT, mode, _WORKER],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert parent.stdout is not None
    return parent, int(parent.stdout.readline())


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


def _cleanup(parent: subprocess.Popen[str], worker: int) -> None:
    for pid in (worker, parent.pid):
        with contextlib.suppress(psutil.NoSuchProcess):
            psutil.Process(pid).kill()
    parent.wait()


def test_a_worker_exits_once_its_parent_is_gone() -> None:
    parent, worker = _start("die")
    try:
        parent.wait(timeout=10)
        assert _wait_gone(worker, within_s=10), "the orphaned worker kept running"
    finally:
        _cleanup(parent, worker)


def test_a_worker_keeps_running_while_its_parent_lives() -> None:
    parent, worker = _start("live")
    try:
        # Many poll intervals: a watch that fires on a live parent fires here.
        time.sleep(1.0)
        assert psutil.Process(worker).status() != psutil.STATUS_ZOMBIE
        assert psutil.pid_exists(worker), "the worker exited with its parent alive"
    finally:
        _cleanup(parent, worker)


def test_parent_gone_compares_against_the_recorded_parent() -> None:
    assert worker_diagnostics.parent_gone(os.getppid()) is False
    assert worker_diagnostics.parent_gone(os.getppid() + 1) is True
