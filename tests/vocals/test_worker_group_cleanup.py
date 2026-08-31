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
    live_group_members,
    process_group_exists,
)
from apps.vocals import cli as vcli

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="POSIX process-group semantics"
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
def zombie_worker() -> Iterator[subprocess.Popen[str]]:
    """A worker that has exited and NOT been waited on: a real zombie group.

    ``start_new_session=True`` makes the child its own group leader, so its
    pgid equals its pid -- the same spawn shape ``run_worker`` uses. We wait
    for the group to stop having any live member (the process has exited) but
    deliberately never call ``proc.wait()``, which is what keeps it a zombie
    and keeps the pgid allocated.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        start_new_session=True,
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
        yield proc
    finally:
        _kill_group(proc.pid)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)


def test_the_fixture_really_builds_a_zombie_only_group(
    zombie_worker: subprocess.Popen[str],
) -> None:
    """Guard the setup: if this is not a zombie group, the test below is void."""
    assert proc_is_unreaped(zombie_worker), "the worker must not have been waited on"
    assert process_group_exists(zombie_worker.pid), "the pgid must still be allocated"
    assert not group_has_live_member(zombie_worker.pid), "nothing may still be running"


def proc_is_unreaped(proc: subprocess.Popen[str]) -> bool:
    """Popen.returncode stays None until something waits; poll() would reap."""
    return proc.returncode is None


def test_terminating_an_all_zombie_group_returns_promptly(
    zombie_worker: subprocess.Popen[str],
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
    started = time.monotonic()
    vcli._terminate_worker_tree(zombie_worker)
    elapsed = time.monotonic() - started

    assert elapsed < vcli.WORKER_TERMINATE_GRACE_S, (
        f"cleanup took {elapsed:.2f}s of the {vcli.WORKER_TERMINATE_GRACE_S}s "
        "grace; it should not wait on a group that holds only zombies"
    )
    assert zombie_worker.returncode == 0, "the worker must be reaped, not abandoned"


def test_a_live_group_is_still_confirmed_dead_before_returning() -> None:
    """The zombie shortcut must not blind the check to a group that IS live.

    A worker that ignores SIGTERM and spawns a child that ignores it too is
    the case the ladder exists for: SIGTERM is not enough, SIGKILL is, and the
    function may not return until nothing in the group is running. If the
    zombie-aware check ever short-circuits on "no live member" too eagerly,
    this leaks a child and fails here rather than in production.
    """
    source = (
        "import signal, subprocess, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "child = subprocess.Popen([sys.executable, '-c',\n"
        "    'import signal, time;"
        " signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        " time.sleep(600)'])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(600)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", source],
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    assert proc.stdout is not None
    child_pid = int(proc.stdout.readline().strip())
    try:
        assert group_has_live_member(proc.pid), "fixture must start out live"

        vcli._terminate_worker_tree(proc)

        # Zombie-aware on purpose: the child is reparented when its leader
        # dies, so for a moment it can still be a corpse awaiting init. A
        # corpse answers kill(pid, 0), which is why the raw pid probe that
        # reads as the obvious assertion here would flake.
        survivors = {proc.pid for proc in live_group_members(proc.pid)}
        assert not survivors, f"the group still has live members: {survivors}"
        assert child_pid not in survivors, "the TERM-resistant child leaked"
    finally:
        _kill_group(proc.pid)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(child_pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)
