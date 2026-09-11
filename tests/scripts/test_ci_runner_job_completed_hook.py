"""ops/ci/runner-hooks/job-completed.sh kills what a finished job left behind.

Linux only (reads /proc). The hook is deployed OUTSIDE the checkout on each
runner host as ACTIONS_RUNNER_HOOK_JOB_COMPLETED; this pins the source it is
deployed from.

Regression lines:
  - if a process with cwd under <runner>/_work is alive after the hook then
    broken
  - if a process outside <runner>/_work is signalled then broken
  - if a leftover that ignores SIGTERM survives then broken
  - if RUNNER_WORKSPACE is unset, or not under a _work dir, then the hook
    must refuse rather than scan the wrong tree
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="reads /proc")

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / "ops" / "ci" / "runner-hooks" / "job-completed.sh"


def _sleeper(cwd: Path, *, ignore_term: bool = False) -> subprocess.Popen:
    if ignore_term:
        code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)"
        argv = [sys.executable, "-c", code]
    else:
        argv = ["sleep", "300"]
    return subprocess.Popen(argv, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _run_hook(env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(HOOK)],
        env={k: v for k, v in os.environ.items() if k != "RUNNER_WORKSPACE"} | env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "_work" / "repo"
    ws.mkdir(parents=True)
    return ws


def test_leftovers_under_work_die_and_neighbours_survive(workspace: Path, tmp_path: Path) -> None:
    """if a cwd-under-_work process survives, or an outside one dies, then broken"""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    leftover = _sleeper(workspace)
    control = _sleeper(elsewhere)
    try:
        result = _run_hook({"RUNNER_WORKSPACE": str(workspace), "RUNNER_NAME": "test"})
        assert result.returncode == 0, result.stderr
        assert f"leftover pid={leftover.pid}" in result.stdout
        assert "leftovers=1 remaining=0" in result.stdout
        assert leftover.wait(timeout=10) == -signal.SIGTERM
        time.sleep(0.5)
        assert control.poll() is None, "a process outside _work was killed"
    finally:
        leftover.kill()
        control.kill()
        control.wait(timeout=10)


def test_a_leftover_that_ignores_sigterm_is_killed(workspace: Path) -> None:
    """if a leftover that ignores SIGTERM survives then broken"""
    stubborn = _sleeper(workspace, ignore_term=True)
    try:
        result = _run_hook({"RUNNER_WORKSPACE": str(workspace)})
        assert result.returncode == 0, result.stderr
        assert "ignored SIGTERM" in result.stdout and "remaining=0" in result.stdout
        assert stubborn.wait(timeout=10) == -signal.SIGKILL
    finally:
        stubborn.kill()


def test_unset_workspace_refuses() -> None:
    """if RUNNER_WORKSPACE is unset then the hook must fail loud"""
    result = _run_hook({})
    assert result.returncode != 0 and "RUNNER_WORKSPACE" in result.stderr


def test_workspace_outside_a_work_dir_refuses(tmp_path: Path) -> None:
    """if the workspace's parent is not _work then the hook must not scan it"""
    result = _run_hook({"RUNNER_WORKSPACE": str(tmp_path / "repo")})
    assert result.returncode != 0 and "not a runner _work directory" in result.stderr
