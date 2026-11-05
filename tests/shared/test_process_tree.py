"""The one process-tree seam, exercised with real process trees on this OS.

Every test spawns a real tree: a root that starts a grandchild and prints its
pid, so the test can check afterwards that EVERY process in it is gone, not
just the one it holds a handle to. Nothing is mocked. The POSIX arm (process
group) runs on macOS and Linux, the Windows arm (psutil parentage plus a
console group) runs on Windows; the tests are written once and run whichever
arm the host has.

[if] any member of a stopped tree survives [then] fail, [else stop].
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager

import psutil
import pytest

from apps.shared.pid_liveness import pid_is_running
from apps.shared.process_tree import (
    ProcessRow,
    ProcessTreeError,
    TreeRoot,
    live_tree_members,
    root_of,
    spawn_tree,
    stop_processes,
    stop_tree,
    tree_pids_by_parentage,
)

pytestmark = pytest.mark.requirement("PROCTREE-7105")

_WINDOWS = sys.platform == "win32"
GRACE_S = 3.0
SLACK_S = 2.0

# The root ignores the polite signal when told to, so the forced rung is
# what has to end it. SIGBREAK is what CTRL_BREAK_EVENT raises on Windows.
_IGNORE_POLITE = (
    "import signal; signal.signal(signal.SIGBREAK if hasattr(signal, 'SIGBREAK') else signal.SIGTERM, signal.SIG_IGN)"
)
_GRANDCHILD = "import time; time.sleep(120)"


def _root_code(*, stubborn: bool, orphan: bool) -> str:
    """A root that starts one grandchild, prints its pid, then waits (or exits)."""
    ignore = _IGNORE_POLITE + "; " if stubborn else ""
    child = (ignore + _GRANDCHILD).replace("'", "\\'")
    tail = "" if orphan else "; time.sleep(120)"
    return (
        f"{ignore}import subprocess, sys, time; "
        f"c = subprocess.Popen([sys.executable, '-c', '{child}']); "
        f"print(c.pid, flush=True){tail}"
    )


@contextmanager
def _owned_tree(
    *, stubborn: bool = False, orphan: bool = False
) -> Iterator[tuple[subprocess.Popen[str], TreeRoot, int]]:
    # An orphan's root runs the BASE interpreter: a venv python.exe on Windows
    # is a launcher with the real interpreter as its child, and that dead
    # middle process would hide the grandchild (the documented limit), so the
    # test would measure the launcher instead of the stale-ppid link.
    interpreter = sys._base_executable if orphan else sys.executable
    proc, root = spawn_tree(
        [interpreter, "-c", _root_code(stubborn=stubborn, orphan=orphan)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        grandchild = int(proc.stdout.readline())
        if orphan:
            proc.wait(timeout=10)
        else:
            time.sleep(0.3)  # let the root install its signal handler
        yield proc, root, grandchild
    finally:
        _hard_cleanup(proc)


def _hard_cleanup(proc: subprocess.Popen[str]) -> None:
    """Kill whatever a failing test left, by parentage, then close the pipe."""
    try:
        leftovers = psutil.Process(proc.pid).children(recursive=True)
        leftovers.append(psutil.Process(proc.pid))
    except psutil.NoSuchProcess:
        leftovers = []
    for leftover in leftovers:
        with contextlib.suppress(psutil.NoSuchProcess):
            leftover.kill()
    proc.wait(timeout=10)
    if proc.stdout is not None:
        proc.stdout.close()


def _wait_reaped(pid: int, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not pid_is_running(pid):
            return True
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            return True
        time.sleep(0.05)
    return False


def test_stop_tree_ends_the_root_and_its_grandchild() -> None:
    """[if] an owned tree is stopped [then] the root AND its grandchild are gone, [else stop]."""
    with _owned_tree() as (proc, root, grandchild):
        assert pid_is_running(grandchild), "fixture: the grandchild must start live"
        stop_tree(root, grace_s=GRACE_S)
        proc.wait(timeout=5)
        assert _wait_reaped(grandchild), f"grandchild {grandchild} outlived stop_tree"
        assert live_tree_members(root) == ()


def test_a_polite_tree_is_not_forced() -> None:
    """[if] the tree exits on the polite signal [then] stop_tree does not force it, [else stop]."""
    with _owned_tree() as (_proc, root, _grandchild):
        outcome = stop_tree(root, grace_s=GRACE_S)
    assert outcome.forced is False, outcome.sentence


def test_a_stubborn_tree_is_forced_inside_the_grace() -> None:
    """[if] the tree ignores the polite signal [then] it is forced within grace_s, [else stop]."""
    with _owned_tree(stubborn=True) as (proc, root, grandchild):
        started = time.monotonic()
        outcome = stop_tree(root, grace_s=GRACE_S)
        took = time.monotonic() - started
        proc.wait(timeout=5)
        assert outcome.forced is True, outcome.sentence
        assert took < GRACE_S + SLACK_S, f"stop_tree took {took:.1f}s"
        assert _wait_reaped(grandchild)


def test_a_tree_whose_root_already_died_is_still_stopped() -> None:
    """[if] the root exited but its grandchild lives [then] stop_tree still ends it, [else stop]."""
    with _owned_tree(orphan=True) as (_proc, root, grandchild):
        assert pid_is_running(grandchild), "fixture: the orphan must start live"
        members = {member.pid for member in live_tree_members(root)}
        assert grandchild in members, f"orphan {grandchild} not found in {members}"
        stop_tree(root, grace_s=GRACE_S)
        assert _wait_reaped(grandchild)


def test_a_process_outside_the_tree_survives() -> None:
    """[if] an unrelated process is running [then] stopping a tree leaves it alone, [else stop]."""
    bystander = subprocess.Popen([sys.executable, "-c", _GRANDCHILD])
    try:
        with _owned_tree(stubborn=True) as (_proc, root, _grandchild):
            assert bystander.pid not in {m.pid for m in live_tree_members(root)}
            stop_tree(root, grace_s=GRACE_S)
        assert bystander.poll() is None, "stop_tree killed a process outside its tree"
    finally:
        bystander.kill()
        bystander.wait(timeout=10)


def test_a_recycled_root_identity_is_refused() -> None:
    """[if] the root pid's start time does not match [then] nothing is signalled, [else stop]."""
    with _owned_tree() as (proc, root, grandchild):
        stranger = TreeRoot(
            pid=root.pid, started_at=root.started_at - 3600.0, console_group=root.console_group, pinned=root.pinned
        )
        with pytest.raises(ProcessTreeError, match="recycled"):
            stop_tree(stranger, grace_s=GRACE_S)
        assert proc.poll() is None and pid_is_running(grandchild)


@pytest.mark.parametrize("flag", ["start_new_session", "creationflags", "process_group"])
def test_spawn_tree_refuses_a_caller_supplied_group_flag(flag: str) -> None:
    """[if] a caller passes its own session or group flag [then] spawn_tree refuses before spawning, [else stop]."""
    with pytest.raises(ProcessTreeError, match=flag):
        spawn_tree([sys.executable, "-c", _GRANDCHILD], **{flag: 0})


@pytest.mark.skipif(not _WINDOWS, reason="Windows-specific: CTRL_BREAK needs the root's console group")
def test_windows_tree_without_a_console_group_is_forced_and_says_so() -> None:
    """[if] a Windows root has no console group of its own [then] it is forced and the outcome names why, [else stop]."""
    with _owned_tree() as (_proc, root, grandchild):
        no_group = TreeRoot(pid=root.pid, started_at=root.started_at, console_group=False, pinned=root.pinned)
        outcome = stop_tree(no_group, grace_s=GRACE_S)
        assert outcome.forced is True
        assert "console group" in outcome.sentence
        assert _wait_reaped(grandchild)


def test_stop_processes_ends_every_listed_process() -> None:
    """[if] listed processes ignore the polite signal [then] stop_processes still leaves none running, [else stop]."""
    # They ignore SIGTERM, so on POSIX only the kill rung can end them. On
    # Windows psutil's terminate() is already TerminateProcess.
    stubborn = f"{_IGNORE_POLITE}; {_GRANDCHILD}"
    sleepers = [subprocess.Popen([sys.executable, "-c", stubborn]) for _ in range(2)]
    time.sleep(0.3)  # let them install the handler
    try:
        survivors = stop_processes([psutil.Process(s.pid) for s in sleepers], grace_s=GRACE_S)
        assert survivors == []
        for sleeper in sleepers:
            sleeper.wait(timeout=5)
    finally:
        for sleeper in sleepers:
            if sleeper.poll() is None:
                sleeper.kill()
                sleeper.wait(timeout=10)


def test_grace_must_be_positive() -> None:
    """[if] grace_s is not positive [then] stop_tree refuses before signalling, [else stop]."""
    with _owned_tree() as (proc, root, _grandchild):
        with pytest.raises(ValueError, match="grace_s"):
            stop_tree(root, grace_s=0)
        assert proc.poll() is None


# The parentage rule, on explicit process-table rows. A live test cannot stage
# pid reuse, so these rows are how the two recycle guards are made to bite.
ROOT_PID, ROOT_START = 100, 1000.0


def test_parentage_finds_children_of_a_dead_root() -> None:
    """[if] the root is gone but a child still names it as parent [then] the child is in the tree, [else stop]."""
    rows = [
        ProcessRow(pid=200, ppid=ROOT_PID, started=ROOT_START + 1),
        ProcessRow(pid=300, ppid=200, started=ROOT_START + 2),
    ]
    assert tree_pids_by_parentage(ROOT_PID, ROOT_START, rows, root_pinned=True) == {200, 300}


def test_parentage_does_not_adopt_orphans_of_an_unpinned_dead_root() -> None:
    """[if] the root is dead and nothing pins its pid [then] no process naming it is adopted, [else stop].

    The pid may have been reused by a stranger that spawned a child and died
    too; that child names the root's pid and passes both start-time guards.
    """
    rows = [
        ProcessRow(pid=200, ppid=ROOT_PID, started=ROOT_START + 1),
        ProcessRow(pid=300, ppid=200, started=ROOT_START + 2),
    ]
    assert tree_pids_by_parentage(ROOT_PID, ROOT_START, rows, root_pinned=False) == set()


def test_parentage_still_walks_a_live_unpinned_root() -> None:
    """[if] an unpinned root is still alive with its recorded start [then] its children are in the tree, [else stop]."""
    rows = [
        ProcessRow(pid=ROOT_PID, ppid=1, started=ROOT_START),
        ProcessRow(pid=200, ppid=ROOT_PID, started=ROOT_START + 1),
    ]
    assert tree_pids_by_parentage(ROOT_PID, ROOT_START, rows, root_pinned=False) == {ROOT_PID, 200}


def test_parentage_skips_a_child_older_than_the_root() -> None:
    """[if] a process names the root pid as parent but predates the root [then] it is not in the tree, [else stop]."""
    rows = [ProcessRow(pid=200, ppid=ROOT_PID, started=ROOT_START - 600)]
    assert tree_pids_by_parentage(ROOT_PID, ROOT_START, rows, root_pinned=True) == set()


def test_parentage_skips_the_children_of_a_stranger_holding_the_root_pid() -> None:
    """[if] a stranger now holds the root pid [then] only children born before it took the pid are ours, [else stop]."""
    stranger_start = ROOT_START + 60
    rows = [
        ProcessRow(pid=ROOT_PID, ppid=1, started=stranger_start),
        ProcessRow(pid=200, ppid=ROOT_PID, started=ROOT_START + 1),
        ProcessRow(pid=201, ppid=ROOT_PID, started=stranger_start + 1),
    ]
    assert tree_pids_by_parentage(ROOT_PID, ROOT_START, rows, root_pinned=True) == {200}


def test_parentage_includes_a_live_root_with_its_recorded_start() -> None:
    """[if] the root pid is live with the recorded start [then] the root itself is in the tree, [else stop]."""
    rows = [ProcessRow(pid=ROOT_PID, ppid=1, started=ROOT_START)]
    assert tree_pids_by_parentage(ROOT_PID, ROOT_START, rows, root_pinned=False) == {ROOT_PID}


@pytest.mark.skipif(not _WINDOWS, reason="Windows-specific: the parent map backs the Windows arm only")
def test_windows_tree_lookup_is_fast_in_a_fresh_process() -> None:
    """[if] a fresh process looks up a tree on Windows [then] it takes under a second, not a full process-table read, [else stop]."""
    code = (
        "import sys, time; from apps.shared.process_tree import spawn_tree, live_tree_members, stop_tree; "
        "proc, root = spawn_tree([sys.executable, '-c', 'import time; time.sleep(60)']); "
        "time.sleep(0.5); t = time.monotonic(); live_tree_members(root); took = time.monotonic() - t; "
        "stop_tree(root, grace_s=3.0); proc.wait(5); print(took)"
    )
    took = float(
        subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60
        ).stdout.strip()
    )
    assert took < 1.0, f"first tree lookup took {took:.2f}s"


def test_root_of_returns_the_root_spawn_tree_recorded() -> None:
    """[if] a handle came from spawn_tree [then] root_of gives back its root, [else stop]."""
    with _owned_tree() as (proc, root, _grandchild):
        assert root_of(proc) == root


def test_root_of_refuses_a_handle_the_seam_did_not_spawn() -> None:
    """[if] a handle was started without spawn_tree [then] root_of refuses it, [else stop]."""
    proc = subprocess.Popen([sys.executable, "-c", _GRANDCHILD])
    try:
        with pytest.raises(ProcessTreeError, match="spawn_tree"):
            root_of(proc)
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_spawn_tree_is_the_only_public_way_to_start_a_tree() -> None:
    """[if] the seam exports its Popen keywords [then] a caller can start a tree root_of refuses, [else stop]."""
    import apps.shared.process_tree as seam

    assert "owner_popen_kwargs" not in seam.__all__
    assert not hasattr(seam, "owner_popen_kwargs")
