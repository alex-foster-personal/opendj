"""Reaping a group whose LEADER is already dead.

A worker that spawns children and then exits leaves the thing a group kill
exists to collect: an orphaned tree, still holding the recorded pgid, with no
leader left to identity-check. The three-way gate can never pass there, so the
reap used to report "pid N is not running" and walk away while the whole tree
kept running.

Single-line intent:
  - if a dead leader with live children reports 'not running' then the tree
    leaks silently and the row claims it was handled
  - if a leaderless group is killed WITHOUT evidence then a recycled pgid
    takes a stranger's process tree down with it
  - if an unverifiable group is skipped quietly then nobody ever learns a
    worker is still out there

Real processes throughout. A simulated process group proves nothing about
process groups.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import psutil
import pytest

from apps.engine_core.jobs.reap import (
    WorkerIdentity,
    group_has_live_member,
    process_group_exists,
    reap,
    reap_group,
)

# The leader spawns a child in its OWN process group (no start_new_session on
# the inner Popen), prints the child's pid, and exits immediately. What is left
# behind is a live group whose leader is gone -- the C4 shape.
_LEADER_SOURCE = """
import subprocess, sys
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
print(child.pid, flush=True)
"""


def _kill_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return


def _leaderless_group() -> tuple[int, int, float]:
    """Return (pgid, surviving child pid, spawn wall-clock).

    The leader is waited on, so it is not even a zombie: pid `pgid` is gone
    outright while the group is still live through the child.
    """
    spawned_at = time.time()
    leader = subprocess.Popen(
        [sys.executable, "-c", _LEADER_SOURCE],
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    assert leader.stdout is not None
    line = leader.stdout.readline()
    assert line.strip(), "leader never reported its child"
    child_pid = int(line.strip())
    leader.wait(timeout=10)  # reap the leader so pid `pgid` is truly gone

    deadline = time.monotonic() + 5
    while psutil.pid_exists(leader.pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    return leader.pid, child_pid, spawned_at


@pytest.fixture
def leaderless() -> tuple[int, int, float]:
    pgid, child_pid, spawned_at = _leaderless_group()
    try:
        yield pgid, child_pid, spawned_at
    finally:
        _kill_pid(child_pid)


def test_the_fixture_really_builds_a_leaderless_live_group(leaderless) -> None:
    """Guard the setup: if this shape is wrong, the tests below prove nothing."""
    pgid, child_pid, _ = leaderless
    assert not psutil.pid_exists(pgid), "the leader is supposed to be gone"
    assert process_group_exists(pgid), "the group is supposed to still be live"
    assert os.getpgid(child_pid) == pgid, "the child must hold the leader's pgid"
    assert group_has_live_member(pgid)


def test_a_dead_leader_with_live_children_still_gets_reaped(leaderless) -> None:
    """The whole tree goes, not just the leader that is already gone.

    Before, identity_mismatch could only ask about the leader, answered "pid N
    is not running", and reap() skipped -- leaving the child running forever
    while the row read as handled.
    """
    pgid, child_pid, spawned_at = leaderless
    identity = WorkerIdentity(
        pgid=pgid,
        argv=(sys.executable, "-c", _LEADER_SOURCE),
        started_at=spawned_at,
    )

    result = reap_group(identity)

    assert result.group_cleared, result.outcome
    assert "reap:" in result.outcome, result.outcome
    assert not group_has_live_member(pgid), result.outcome
    assert not psutil.pid_exists(child_pid), (
        f"the orphaned child survived the reap: {result.outcome}"
    )


def test_a_leaderless_group_that_predates_the_spawn_is_refused(
    leaderless,
) -> None:
    """A recycled pgid must not take a stranger's tree down with it.

    Same live group, but the row claims a worker that started an hour after
    everything in it. No survivor can be a descendant of that worker, so the
    only safe answer is to refuse -- loudly, in the outcome sentence.
    """
    pgid, child_pid, spawned_at = leaderless
    identity = WorkerIdentity(
        pgid=pgid,
        argv=(sys.executable, "-c", _LEADER_SOURCE),
        started_at=spawned_at + 3600,
    )

    result = reap_group(identity)

    assert "reap skipped" in result.outcome, result.outcome
    assert "recycled" in result.outcome, result.outcome
    assert result.group_cleared, "nothing of ours is in a recycled group"
    assert psutil.pid_exists(child_pid), (
        f"an unproven group was killed anyway: {result.outcome}"
    )


def test_a_dead_leader_with_no_survivors_is_simply_gone(leaderless) -> None:
    """Nothing left to kill is a clean skip, not a refusal to retry forever."""
    pgid, child_pid, spawned_at = leaderless
    _kill_pid(child_pid)
    deadline = time.monotonic() + 5
    while group_has_live_member(pgid) and time.monotonic() < deadline:
        time.sleep(0.02)

    result = reap_group(
        WorkerIdentity(pgid=pgid, argv=("x",), started_at=spawned_at)
    )
    assert "reap skipped" in result.outcome, result.outcome
    assert result.group_cleared, result.outcome


def test_a_pgid_of_zero_is_refused_before_any_signal() -> None:
    """killpg(0, sig) signals the ENGINE'S OWN group. Never send it."""
    result = reap_group(WorkerIdentity(pgid=0, argv=("x",), started_at=1.0))
    assert "refused" in result.outcome, result.outcome
    assert result.group_cleared
    assert reap(WorkerIdentity(pgid=-1, argv=("x",), started_at=1.0)).startswith(
        "reap skipped"
    )


def test_a_live_stranger_leader_is_left_alone() -> None:
    """The existing leader gate still refuses; route 3 must not weaken it."""
    stranger = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        result = reap_group(
            WorkerIdentity(
                pgid=stranger.pid,
                argv=("/definitely/not/what/is/running",),
                started_at=time.time(),
            )
        )
        assert "reap skipped" in result.outcome, result.outcome
        assert "does not match" in result.outcome, result.outcome
        assert stranger.poll() is None, "a stranger's group was killed"
    finally:
        _kill_pid(stranger.pid)
        stranger.wait(timeout=10)
