"""scripts/ci_host_lock.sh serializes and REPORTS how long the loser waited.

Linux only (`flock`). A suite queued behind another job's copy of itself is
indistinguishable from a slow suite in the step timing; the printed wait is
what lets the CI lane tell the two apart.

Regression lines:
  - if two holders of one lock name overlap then broken
  - if the loser's printed wait is under the holder's run time then the
    number is not the wait and every reading built on it is wrong
  - if the lock outlives the command, or the command runs without it, then
    broken
  - if a timeout is reported as success then a stuck job looks busy
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="needs flock(1)")

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "ci_host_lock.sh"


def _run(
    name: str, *cmd: str, lock_dir: Path, timeout_s: str = "60"
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(SCRIPT), name, *cmd],
        env={
            **os.environ,
            "MDT_CI_HOST_LOCK_DIR": str(lock_dir),
            "MDT_CI_HOST_LOCK_TIMEOUT_S": timeout_s,
        },
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _start_holder(name: str, seconds: str, lock_dir: Path) -> subprocess.Popen:
    """Start a holder and return only once it has PRINTED its acquisition.

    Popen returning proves nothing about the child having reached flock; on a
    loaded runner the contender could win the race and the test would fail
    with the script correct. The script prints its acquisition line before it
    execs the command, so that line is the readiness signal.
    """
    holder = subprocess.Popen(
        [str(SCRIPT), name, "sleep", seconds],
        env={**os.environ, "MDT_CI_HOST_LOCK_DIR": str(lock_dir)},
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    first = holder.stdout.readline()
    assert "acquired after 0s" in first, first
    return holder


def _waited(out: str) -> float:
    match = re.search(r"\[host-lock\] \S+ acquired after ([0-9.]+)s", out)
    assert match, out
    return float(match.group(1))


def test_the_loser_waits_for_the_holder_and_says_how_long(tmp_path: Path) -> None:
    """[if] the second holder overlaps, or under-reports its wait [then] fail, [else stop]."""
    holder = _start_holder("suite", "3", tmp_path)
    started = time.monotonic()
    loser = _run("suite", "echo", "ran", lock_dir=tmp_path)
    elapsed = time.monotonic() - started
    holder.wait(timeout=30)
    assert loser.returncode == 0, loser.stdout + loser.stderr
    assert loser.stdout.rstrip().endswith("ran"), loser.stdout
    assert elapsed >= 2.0, f"the loser did not wait for the holder ({elapsed:.1f}s)"
    assert _waited(loser.stdout) >= 2.0, loser.stdout


def test_an_uncontended_lock_costs_nothing_and_runs_the_command(tmp_path: Path) -> None:
    """[if] an uncontended lock ever reports a wait [then] fail, [else stop].

    Non-blocking acquisition first, so no wall-clock second boundary can turn
    a free lock into "1s" (Codex reproduced 12 in 500 with two date +%s reads)."""
    result = _run("free", "sh", "-c", "echo held", lock_dir=tmp_path)
    assert result.returncode == 0 and "held" in result.stdout
    assert _waited(result.stdout) == 0.0, result.stdout


def test_a_timeout_fails_loud_instead_of_running_unlocked(tmp_path: Path) -> None:
    """[if] a lock held past the timeout runs the command or exits 0 [then] fail, [else stop]."""
    holder = _start_holder("stuck", "6", tmp_path)
    try:
        loser = _run("stuck", "echo", "must-not-run", lock_dir=tmp_path, timeout_s="1")
        assert loser.returncode != 0 and "must-not-run" not in loser.stdout
        assert "not acquired within 1s" in loser.stderr
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_the_command_exit_status_is_the_step_status(tmp_path: Path) -> None:
    result = _run("status", "sh", "-c", "exit 7", lock_dir=tmp_path)
    assert result.returncode == 7
