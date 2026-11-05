"""Terminating a vocal worker whose process group holds only zombies.

The shape: the worker exits on its own (a crash, or a race with the timeout
that is about to kill it) and nobody has waited on it yet. It is a ZOMBIE --
dead, but still holding its pgid, because the pgid stays allocated until the
parent reaps it. On macOS ``killpg`` answers that group with EPERM, not ESRCH.

Measured on this machine (darwin), the old code failed that group TWICE over:

  - ``killpg(pgid, SIGTERM)`` itself took EPERM. Only ProcessLookupError was
    caught there, so it fell through to the blanket handler and raised
    WorkerCleanupError("cleanup is unverified") in 0.01s.
  - and had the SIGTERM landed, ``_posix_process_group_exists`` read the same
    EPERM as "the group exists", so the confirm loop polled a corpse for the
    full 2.00s grace and then raised "survived SIGKILL".

Both verdicts were wrong in the same direction: the worker was already dead,
and the caller's claim got pinned ``manual_recovery_required`` for a tree that
did not exist.

Single-line intent:
  - if terminating an all-zombie group burns the full grace then every worker
    that dies just before its timeout costs 2s of dead wall clock
  - if it then raises WorkerCleanupError then the track's claim is stranded
    non-expiring and needs a human, for a worker that already exited cleanly
  - if the group check ignored zombies entirely then a group with a LIVE
    member would look dead and a real leak would go unreported

Real processes throughout. Monkeypatching os.killpg proves what the patch
returns, not what the kernel does with a corpse -- which is the entire bug.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator

import pytest

from apps.shared.process_groups import (
    group_has_live_member,
    process_group_exists,
)
from apps.shared.process_tree import TreeRoot, live_tree_members, spawn_tree
from apps.vocals import cli as vcli

# Windows has no zombies (a dead process is gone once its handles close), so
# the all-zombie-group regression exists only on POSIX.
posix_zombies_only = pytest.mark.skipif(
    os.name == "nt", reason="POSIX-specific: zombies and process groups do not exist on Windows"
)


def _kill_group(pgid: int) -> None:
    """Best-effort teardown.

    killpg on a group that has already dissolved raises ESRCH, and on a group
    that is all zombies macOS raises EPERM instead. Both mean there is nothing
    left to kill, which is what teardown wanted.
    """
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


@pytest.fixture
def zombie_worker() -> Iterator[tuple[subprocess.Popen[str], TreeRoot]]:
    """A worker that has exited and NOT been waited on: a real zombie group.

    ``start_new_session=True`` makes the child its own group leader, so its
    pgid equals its pid -- the same spawn shape ``run_worker`` uses. We wait
    for the group to stop having any live member (the process has exited) but
    deliberately never call ``proc.wait()``, which is what keeps it a zombie
    and keeps the pgid allocated.
    """
    proc, root = spawn_tree(
        [sys.executable, "-c", "pass"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    deadline = time.monotonic() + 10
    while group_has_live_member(proc.pid):
        if time.monotonic() >= deadline:
            raise AssertionError("the worker never finished exiting")
        time.sleep(0.02)
    try:
        yield proc, root
    finally:
        _kill_group(proc.pid)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)


@posix_zombies_only
def test_the_fixture_really_builds_a_zombie_only_group(
    zombie_worker: tuple[subprocess.Popen[str], TreeRoot],
) -> None:
    """Guard the setup: if this is not a zombie group, the test below is void."""
    proc, _root = zombie_worker
    assert proc_is_unreaped(proc), "the worker must not have been waited on"
    assert process_group_exists(proc.pid), "the pgid must still be allocated"
    assert not group_has_live_member(proc.pid), "nothing may still be running"


def proc_is_unreaped(proc: subprocess.Popen[str]) -> bool:
    """Popen.returncode stays None until something waits; poll() would reap."""
    return proc.returncode is None


@posix_zombies_only
def test_terminating_an_all_zombie_group_returns_promptly(
    zombie_worker: tuple[subprocess.Popen[str], TreeRoot],
) -> None:
    """No raise, and nowhere near the grace budget.

    Before the fix this raised WorkerCleanupError on the EPERM that macOS
    returns for an all-zombie group. The elapsed-time assertion guards the
    other half of the same bug -- the confirm loop that polls a corpse until
    the grace runs out -- so a fix that stopped the raise but kept the spin
    would still fail here. The budget is asserted against the real constant
    rather than a hardcoded number, so a future grace change cannot quietly
    turn this test green for the wrong reason.
    """
    proc, root = zombie_worker
    started = time.monotonic()
    vcli._terminate_worker_tree(proc, root)
    elapsed = time.monotonic() - started

    assert elapsed < vcli.WORKER_TERMINATE_GRACE_S, (
        f"cleanup took {elapsed:.2f}s of the {vcli.WORKER_TERMINATE_GRACE_S}s "
        "grace; it should not wait on a group that holds only zombies"
    )
    assert proc.returncode == 0, "the worker must be reaped, not abandoned"


def test_a_live_group_is_still_confirmed_dead_before_returning() -> None:
    """[if] a worker and its child both ignore the polite signal [then] terminating it still leaves nothing running, [else stop].

    The zombie shortcut must not blind the check to a tree that IS live. A
    worker that ignores the polite signal and spawns a child that ignores it
    too is the case the ladder exists for: the polite rung is not enough, the
    forced rung is, and the function may not return until nothing in the tree
    is running. Runs on both arms: SIGTERM is what POSIX ignores, SIGBREAK
    (what CTRL_BREAK raises) is what Windows ignores.
    """
    ignore = "import signal; signal.signal(getattr(signal, 'SIGBREAK', signal.SIGTERM), signal.SIG_IGN)"
    child_source = ignore + "; import time; time.sleep(600)"
    source = "\n".join(
        [
            "import subprocess, sys, time",
            ignore,
            f"child = subprocess.Popen([sys.executable, '-c', {child_source!r}])",
            "print(child.pid, flush=True)",
            "time.sleep(600)",
        ]
    )
    proc, root = spawn_tree(
        [sys.executable, "-c", source],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    assert proc.stdout is not None
    child_pid = int(proc.stdout.readline().strip())
    try:
        time.sleep(0.3)  # let both install their handler
        assert live_tree_members(root), "fixture must start out live"

        vcli._terminate_worker_tree(proc, root)

        survivors = {member.pid for member in live_tree_members(root)}
        assert not survivors, f"the tree still has live members: {survivors}"
        assert child_pid not in survivors, "the TERM-resistant child leaked"
    finally:
        for leftover in live_tree_members(root):
            with contextlib.suppress(OSError):
                leftover.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)
