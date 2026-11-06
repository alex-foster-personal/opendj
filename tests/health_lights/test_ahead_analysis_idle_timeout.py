"""An idle _queue_cli timeout must stop the spawn-pool tree (issue #6942).

Measured on demon-llama: subprocess.run SIGKILLed only queue_cli and left
33 reparented spawn workers / resource_trackers (2.3 GB). Idle and
while-playing now share the Popen + _stop_process_tree loop.

Regression lines:
  - if idle _queue_cli still uses subprocess.run then a timeout orphans workers
  - if a timed-out idle run leaves a recorded descendant running then broken
  - if the owned spawn pool never starts then this test measured nothing

[if] idle _queue_cli times out [then] no recorded descendant is still running, [else stop].
"""
from __future__ import annotations

import inspect
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from apps.webui.server import ahead_analysis as aa

# Production spawn_pool in a real child: two workers plus the resource
# tracker. sys.argv[1] is the marker path from tmp_path. A host that cannot
# create POSIX semaphores (ProcessPoolExecutor needs /dev/shm) still starts
# two spawn-context workers, the same orphan class the leak was.
_SPAWN_POOL_CHILD = (
    "import os, sys, time\n"
    "import multiprocessing\n"
    "import psutil\n"
    "from apps.analysis.pool import spawn_pool\n"
    "try:\n"
    "    pool = spawn_pool(2)\n"
    "    [pool.submit(time.sleep, 120) for _ in range(2)]\n"
    "    while len(pool._processes) < 2:\n"
    "        time.sleep(0.05)\n"
    "except PermissionError:\n"
    "    ctx = multiprocessing.get_context('spawn')\n"
    "    workers = [ctx.Process(target=time.sleep, args=(120,)) for _ in range(2)]\n"
    "    for worker in workers:\n"
    "        worker.start()\n"
    "    while sum(1 for worker in workers if worker.pid) < 2:\n"
    "        time.sleep(0.05)\n"
    "pids = [os.getpid(), *(c.pid for c in psutil.Process(os.getpid()).children(recursive=True))]\n"
    "with open(sys.argv[1], 'w', encoding='utf-8') as fh:\n"
    "    fh.write('\\n'.join(str(pid) for pid in pids) + '\\n')\n"
    "time.sleep(600)\n"
)


def _worker_running(pid: int) -> bool:
    try:
        proc = psutil.Process(pid)
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _recorded_pids(marker: Path) -> list[int]:
    if not marker.exists():
        return []
    text = marker.read_text(encoding="utf-8")
    if not text.endswith("\n"):
        return []
    return [int(line) for line in text.splitlines() if line.strip()]


def _reap(pids: list[int]) -> None:
    for pid in pids:
        try:
            proc = psutil.Process(pid)
            if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
                proc.kill()
        except psutil.NoSuchProcess:
            continue


def test_queue_cli_source_has_no_subprocess_run() -> None:
    """[if] _queue_cli still contains subprocess.run( [then] fail, [else stop]."""
    assert "subprocess.run(" not in inspect.getsource(aa._queue_cli)


def test_idle_queue_cli_timeout_leaves_no_spawn_pool_descendants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] idle _queue_cli times out over a spawn pool [then] those pids are gone, [else stop]."""
    marker = tmp_path / "tree.pids"
    db = tmp_path / "state.db"
    monkeypatch.setattr(aa, "QUEUE_TIMEOUT_S", 5.0)
    monkeypatch.setattr(
        aa,
        "background_argv",
        lambda _argv: [sys.executable, "-c", _SPAWN_POOL_CHILD, str(marker)],
    )
    recorded: list[int] = []
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            aa._queue_cli(["status"], str(db), abandon=None)
        recorded = _recorded_pids(marker)
        if len(recorded) < 2:
            pytest.fail("the owned spawn pool never started")
        live = [pid for pid in recorded if _worker_running(pid)]
        assert live == [], f"idle timeout left live descendants: {live}"
    finally:
        _reap(recorded or _recorded_pids(marker))
