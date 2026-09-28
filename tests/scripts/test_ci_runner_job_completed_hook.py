"""ops/ci/runner-hooks/job-completed.sh kills what a finished job left behind.

[if] a finished job leaves a process alive [then] fail, [else stop].

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
  - if a process OUTSIDE _work carrying this job's RUNNER_NAME survives then
    broken
  - if a process carrying ANOTHER runner's RUNNER_NAME is signalled then broken
  - if the report line's leftovers/kills disagree with what was killed then
    broken
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.skipif(sys.platform != "linux", reason="reads /proc"),
    pytest.mark.requirement("DEVOPS-17"),
]

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / "ops" / "ci" / "runner-hooks" / "job-completed.sh"


# Every hook run gets a runner name no real process carries. On a CI runner
# os.environ holds the REAL job's RUNNER_NAME, and the hook kills whatever
# carries its runner name, so inheriting it would aim the hook at this very
# job's pytest workers.
TEST_RUNNER_NAME = f"hook-under-test-{uuid.uuid4().hex[:8]}"


def _sleeper(
    cwd: Path, *, ignore_term: bool = False, runner_name: str | None = None
) -> subprocess.Popen:
    if ignore_term:
        code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)"
        argv = [sys.executable, "-c", code]
    else:
        argv = ["sleep", "300"]
    env = {k: v for k, v in os.environ.items() if k != "RUNNER_NAME"}
    if runner_name is not None:
        env["RUNNER_NAME"] = runner_name
    return subprocess.Popen(
        argv, cwd=cwd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def _run_hook(env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(HOOK)],
        env={k: v for k, v in os.environ.items() if k != "RUNNER_WORKSPACE"}
        | {"RUNNER_NAME": TEST_RUNNER_NAME}
        | env,
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
        result = _run_hook({"RUNNER_WORKSPACE": str(workspace)})
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


def test_a_leftover_outside_work_with_my_runner_name_dies(workspace: Path, tmp_path: Path) -> None:
    """if a process outside _work carrying this RUNNER_NAME survives, or one
    carrying ANOTHER runner's name (or none) dies, then broken"""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    report = tmp_path / "report.jsonl"
    mine = _sleeper(elsewhere, runner_name=TEST_RUNNER_NAME)
    other_runner = _sleeper(elsewhere, runner_name=f"{TEST_RUNNER_NAME}-other")
    unnamed = _sleeper(elsewhere)
    try:
        result = _run_hook({"RUNNER_WORKSPACE": str(workspace), "MDT_CI_HOOK_REPORT": str(report)})
        assert result.returncode == 0, result.stderr
        assert f"leftover pid={mine.pid}" in result.stdout
        assert "leftovers=1 remaining=0" in result.stdout, result.stdout
        assert mine.wait(timeout=10) == -signal.SIGTERM
        time.sleep(0.5)
        assert other_runner.poll() is None, "a process carrying ANOTHER runner's name was killed"
        assert unnamed.poll() is None, "a process with no runner name outside _work was killed"
        line = json.loads(report.read_text().splitlines()[-1])
        assert line["runner"] == TEST_RUNNER_NAME
        assert line["leftovers"] == 1 and line["remaining"] == 0
        assert [k["pid"] for k in line["kills"]] == [mine.pid]
    finally:
        for proc in (mine, other_runner, unnamed):
            proc.kill()
            proc.wait(timeout=10)
