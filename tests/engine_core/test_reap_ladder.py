"""The kill ladder costs what it says it costs.

``terminate_group`` waited ``grace_s`` after SIGTERM and then ``grace_s``
again after SIGKILL, so its true worst case was twice the documented grace.
Boot recovery calls it in a SERIAL loop on the synchronous boot path, one row
after another, so the overrun was paid per orphaned row while the engine had
not finished starting (C17).

The two rungs now share ONE deadline. SIGTERM gets the bulk of it and the
SIGKILL confirm keeps a reserve, because a kill is not instantaneous either --
the kernel still has to schedule the exit and somebody still has to reap it,
and handing the whole grace to SIGTERM would turn every stubborn group into a
raise instead of a kill.

Real processes: a group that traps SIGTERM and ignores it is the only honest
way to make the first rung actually spend its budget.

Single-line intent:
  - if the ladder can outlast the grace it documents then boot recovery's
    serial loop costs twice what the docstring promises, per row
  - if SIGTERM stops getting a real share of the budget then every cancel is
    effectively a SIGKILL and workers lose their chance to exit cleanly
  - if a group that exits on SIGTERM does not return promptly then the grace
    became a floor rather than a ceiling
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
import time
from collections.abc import Iterator

import psutil
import pytest

from apps.engine_core.jobs.reap import (
    WORKER_KILL_CONFIRM_S,
    WorkerIdentity,
    terminate_group,
    worker_tree_has_live_member,
)

# A recorded worker carries no console group of its own on Windows, so its
# reap is forced from the first instant: there is no polite rung to measure.
posix_polite_rung_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-specific: a recorded Windows worker has no console group, so its reap has no polite rung",
)

# Traps SIGTERM and keeps running, so the first rung has to spend its whole
# budget before the ladder escalates. It cannot trap SIGKILL, so the second
# rung always succeeds -- which is what makes the elapsed time readable.
_STUBBORN = "; ".join([
    "import signal, sys, time",
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
    "print('ready', flush=True)",
    "time.sleep(600)",
])

_POLITE = "import sys, time; print('ready', flush=True); time.sleep(600)"

_GRACE_S: float = 2.0


def _leader(source: str) -> tuple[subprocess.Popen[str], WorkerIdentity]:
    """A worker spawned the way the runner spawns one, confirmed ready to be signalled.

    Waiting for the ready line matters: signalling before the trap is
    installed would measure a race, not the ladder.
    """
    argv = [sys.executable, "-c", source]
    proc = subprocess.Popen(
        argv,
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "ready", "the leader never started"
    return proc, WorkerIdentity.capture(proc.pid, argv)


def _kill_leftovers(proc: subprocess.Popen[str]) -> None:
    """Kill whatever a failing test left, by parentage, then reap the handle."""
    try:
        root = psutil.Process(proc.pid)
        leftovers = [*root.children(recursive=True), root]
    except psutil.NoSuchProcess:
        leftovers = []
    for leftover in leftovers:
        with contextlib.suppress(psutil.NoSuchProcess):
            leftover.kill()
    proc.wait(timeout=10)


@pytest.fixture
def stubborn() -> Iterator[WorkerIdentity]:
    proc, identity = _leader(_STUBBORN)
    try:
        yield identity
    finally:
        _kill_leftovers(proc)


@pytest.fixture
def polite() -> Iterator[WorkerIdentity]:
    proc, identity = _leader(_POLITE)
    try:
        yield identity
    finally:
        _kill_leftovers(proc)


def test_the_whole_ladder_fits_inside_one_grace(stubborn: WorkerIdentity) -> None:
    """TERM plus KILL is bounded by grace_s, not by 2 x grace_s.

    Before, the SIGTERM rung alone consumed the full grace and the SIGKILL
    rung then started a second one, so this group -- which never yields to
    SIGTERM -- took longer than the documented budget all by itself, without
    even reaching the pathological case the old worst case described.
    """
    started = time.monotonic()
    outcome = terminate_group(stubborn.pgid, started_at=stubborn.started_at, grace_s=_GRACE_S)
    elapsed = time.monotonic() - started

    assert elapsed < _GRACE_S, (
        f"the ladder took {elapsed:.2f}s, past the {_GRACE_S:.0f}s grace it "
        f"documents: {outcome}"
    )
    assert not worker_tree_has_live_member(stubborn), outcome
    assert "killed" in outcome or "forced" in outcome, outcome


@posix_polite_rung_only
def test_sigterm_still_gets_a_real_share_of_the_budget(stubborn: WorkerIdentity) -> None:
    """Bounding the total must not collapse the ladder into an instant kill.

    A worker that would have exited cleanly on SIGTERM has to be given the
    time to do it, so the reserve the confirm keeps is a slice of the grace,
    never all of it.
    """
    started = time.monotonic()
    terminate_group(stubborn.pgid, started_at=stubborn.started_at, grace_s=_GRACE_S)
    elapsed = time.monotonic() - started

    expected_term_budget = _GRACE_S - min(WORKER_KILL_CONFIRM_S, _GRACE_S / 2)
    assert elapsed >= expected_term_budget * 0.8, (
        f"SIGTERM was only given {elapsed:.2f}s of the {_GRACE_S:.0f}s grace; "
        "a clean exit never had a chance"
    )


@posix_polite_rung_only
def test_a_group_that_exits_on_sigterm_returns_at_once(polite: WorkerIdentity) -> None:
    """The grace is a ceiling, not a delay every cancel pays."""
    started = time.monotonic()
    outcome = terminate_group(polite.pgid, started_at=polite.started_at, grace_s=_GRACE_S)
    elapsed = time.monotonic() - started

    assert elapsed < _GRACE_S / 2, f"waited {elapsed:.2f}s for a clean exit"
    assert "exited on SIGTERM" in outcome, outcome
