"""The electron-dev launch plan: isolation from the installed app, per-worktree
ports and profiles, and headless display selection (scripts/electron_dev.py)."""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.scripts.electron_dev_helpers import (
    _group_with_a_reaped_leader,
)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups; Windows has none to reuse")
def test_a_holder_that_leaves_before_it_is_stopped_proves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The group is checked, then the holder exits before the signal (Sol P1 on #6830).

    Here the holder really is killed inside the first ownership check, which is the
    window the stop closes: the re-check after the stop must refuse the group signal.
    """
    import psutil

    import scripts.electron_dev as dev

    leader, (holder, other) = _group_with_a_reaped_leader(tmp_path)
    real = dev.group_held_by
    checks: list[bool] = []

    def racing(pgid: int, procs: list[psutil.Process], **kw: bool) -> bool:
        held = real(pgid, procs, **kw)
        if not checks:
            holder.kill()
            holder.wait(10)
        checks.append(held)
        return held

    sent: list[bool] = []
    monkeypatch.setattr(dev, "group_held_by", racing)
    monkeypatch.setattr(dev, "_signal", lambda process, *, force: sent.append(force))
    try:
        assert not dev._signal_pinned(leader, [holder], force=False, groups=True)
        assert checks[0], "control: the holder did hold the group at the first check"
        assert sent == []
        assert other.is_running()
    finally:
        for member in (holder, other):
            with contextlib.suppress(psutil.NoSuchProcess):
                member.kill()
                member.wait(10)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups; Windows has none to reuse")
def test_a_held_group_is_signalled_while_its_holder_is_stopped_and_the_holder_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control for the pin: a group a tracked member still holds is signalled, with that
    member stopped at the moment of the signal, and the member runs again afterwards."""
    import time

    import psutil

    import scripts.electron_dev as dev

    leader, (holder, other) = _group_with_a_reaped_leader(tmp_path)
    states: list[str] = []
    monkeypatch.setattr(dev, "_signal", lambda process, *, force: states.append(holder.status()))
    try:
        assert dev._signal_pinned(leader, [holder], force=False, groups=True)
        assert states == [psutil.STATUS_STOPPED]
        # SIGCONT lands asynchronously too, so the resume is awaited rather than sampled once.
        deadline = time.monotonic() + 5
        while holder.status() == psutil.STATUS_STOPPED and time.monotonic() < deadline:
            time.sleep(0.01)
        assert holder.status() != psutil.STATUS_STOPPED
    finally:
        for member in (holder, other):
            with contextlib.suppress(psutil.NoSuchProcess):
                member.kill()
                member.wait(10)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups; Windows has none to reuse")
def test_a_holder_that_never_reports_stopped_is_not_trusted_to_pin_the_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SIGSTOP lands asynchronously: until the holder reports stopped it can still leave the
    group and free the id, so the group is not signalled on its word."""
    import psutil

    import scripts.electron_dev as dev

    leader, (holder, other) = _group_with_a_reaped_leader(tmp_path)
    sent: list[bool] = []
    monkeypatch.setattr(dev, "_signal", lambda process, *, force: sent.append(force))
    monkeypatch.setattr(dev, "PIN_STOP_TIMEOUT_S", 0.2)
    monkeypatch.setattr(psutil.Process, "suspend", lambda self: None)  # the stop never lands
    try:
        assert not dev._signal_pinned(leader, [holder], force=False, groups=True)
        assert sent == []
        assert dev._running(holder), "control: the holder really was still running in the group"
    finally:
        for member in (holder, other):
            with contextlib.suppress(psutil.NoSuchProcess):
                member.kill()
                member.wait(10)


def test_orphans_are_live_children_of_the_leader_started_after_it() -> None:
    """Windows keeps a dead parent's pid as ppid, so that is how a late descendant is found."""
    import scripts.electron_dev as dev

    procs: list[dict[str, Any]] = [
        {"pid": 11, "ppid": 7, "create_time": 105.0},  # started by the leader after it spawned
        {"pid": 12, "ppid": 7, "create_time": 99.0},  # older than the leader: the pid was reused
        {"pid": 13, "ppid": 8, "create_time": 106.0},  # someone else's child
        {"pid": 14, "ppid": 7, "create_time": 107.0},  # tracked, so _stop already handled it
        {"pid": 15, "ppid": 7, "create_time": None},  # start time unreadable: not provably after
        {"pid": 16, "ppid": 11, "create_time": 108.0},  # nested under an orphan
        {"pid": 17, "ppid": 14, "create_time": 108.0},  # nested under a tracked child
        {"pid": 18, "ppid": 12, "create_time": 108.0},  # under the reused pid: not ours
    ]
    assert dev.orphans_of(7, 100.0, {14}, procs) == [11, 16, 17]


@pytest.mark.skipif(sys.platform == "win32", reason="an unreaped exited child shows as a zombie only on POSIX")
def test_remembering_a_tree_never_reaps_its_leader() -> None:
    """An exited leader is read before it is reaped, so what it started since the last
    snapshot is still found by its pid; the caller's poll reaps it after (Sol P1 on #6830)."""
    import time

    import psutil

    import scripts.electron_dev as dev

    leader = dev._spawn([sys.executable, "-c", "pass"], service="test")
    try:
        zombie = psutil.Process(leader.pid)
        deadline = time.monotonic() + 20
        while zombie.status() != psutil.STATUS_ZOMBIE and time.monotonic() < deadline:
            time.sleep(0.01)
        assert zombie.status() == psutil.STATUS_ZOMBIE
        dev.remember_tree(leader)
        assert leader.returncode is None
        assert leader.pid in dev._REMEMBERED.get(leader.pid, {})
        # Control: once reaped, the pid is no longer ours and nothing is read from it.
        leader.wait(20)
        assert dev._live_tree(leader) == []
    finally:
        dev._REMEMBERED.pop(leader.pid, None)
        leader.wait(20)


def test_a_windows_leader_reaped_with_a_child_left_has_it_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without groups, a leader that exited before `_stop` leaves only its children's ppid to
    find them by; the open handle keeps that pid ours, so they are stopped (Sol P1 on #6830)."""
    import subprocess

    import psutil

    import scripts.electron_dev as dev

    leader = dev._spawn([sys.executable, "-c", "pass"], service="test")
    leader.wait(20)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    older = psutil.Process(os.getpid())  # started before the leader: a reused pid, never ours
    listing: list[dict[str, Any]] = []
    real = dev.orphans_of
    seen: list[tuple[int, float, set[int]]] = []

    def fake(leader_pid: int, since: float, exclude: set[int], procs: object = None) -> list[int]:
        seen.append((leader_pid, since, exclude))
        return real(leader_pid, since, exclude, listing)

    monkeypatch.setattr(dev, "orphans_of", fake)
    before = len(dev._CLEANUP_FAILURES)
    try:
        dev._stop(leader, "test leader", grace_s=0.2, groups=False)
        assert len(dev._CLEANUP_FAILURES) == before, "control: a leader that left nothing behind is no failure"
        assert seen and seen[0][0] == leader.pid and seen[0][1] == dev._SPAWNED_AT[leader.pid] > 0

        # The listing saw the pid as started after the leader, but it names an older process
        # now: re-checked against its real start time, it is left alone.
        listing.append({"pid": older.pid, "ppid": leader.pid, "create_time": dev._SPAWNED_AT[leader.pid] + 1})
        dev._stop(leader, "test leader", grace_s=0.2, groups=False)
        assert older.is_running()

        listing[:] = [{"pid": child.pid, "ppid": leader.pid, "create_time": psutil.Process(child.pid).create_time()}]
        dev._stop(leader, "test leader", grace_s=5.0, groups=False)
        assert child.wait(10) is not None
        assert len(dev._CLEANUP_FAILURES) == before, "a stopped orphan is no failure"

        # Control: with groups, the POSIX checks own this case and the ppid scan never runs.
        seen.clear()
        dev._stop(leader, "test leader", grace_s=0.2, groups=True)
        assert seen == []
    finally:
        del dev._CLEANUP_FAILURES[before:]
        child.kill()
        child.wait(10)


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX")
def test_a_helper_started_after_the_last_snapshot_is_stopped_with_its_exited_leader(tmp_path: Path) -> None:
    """The leader starts a helper after the last `remember_tree` and exits; the watch loop
    notices, and the helper is stopped rather than only reported (Sol P1 on #6830)."""
    import time

    import psutil

    import scripts.electron_dev as dev

    ready = tmp_path / "helper.pid"
    src = (
        "import pathlib, subprocess, sys\n"
        "kid = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"pathlib.Path({str(ready)!r}).write_text(str(kid.pid))\n"
    )
    leader = dev._spawn([sys.executable, "-c", src], service="test")
    before = len(dev._CLEANUP_FAILURES)
    helper: psutil.Process | None = None
    try:
        deadline = time.monotonic() + 20
        while dev.dead_service([(leader, "backend")]) is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert leader.returncode == 0, "the watch loop saw the leader exit"
        helper = psutil.Process(int(ready.read_text()))
        assert helper.is_running() and helper.pid in dev._REMEMBERED.get(leader.pid, {})
        dev._stop(leader, "test leader", grace_s=5.0, groups=True)
        helper.wait(10)
        assert not dev._running(helper)
        assert len(dev._CLEANUP_FAILURES) == before
    finally:
        del dev._CLEANUP_FAILURES[before:]
        dev._REMEMBERED.pop(leader.pid, None)
        if helper is not None:
            with contextlib.suppress(psutil.NoSuchProcess):
                helper.kill()


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX")
def test_a_running_leader_is_never_reaped_by_the_exit_check() -> None:
    """Control: `reap_exited` neither polls nor remembers a leader that is still running."""
    import scripts.electron_dev as dev

    leader = dev._spawn([sys.executable, "-c", "import time; time.sleep(30)"], service="test")
    try:
        assert dev.reap_exited(leader) is None
        assert leader.pid not in dev._REMEMBERED
    finally:
        leader.kill()
        leader.wait(10)


def test_a_tree_stop_that_cannot_reach_the_tree_fails_the_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing or failed taskkill leaves untracked descendants unreached, so it fails the run (Sol P1 on #6830)."""
    import subprocess
    import sys

    import scripts.electron_dev as dev

    before = len(dev._CLEANUP_FAILURES)
    leader = subprocess.Popen([sys.executable, "-c", "pass"])
    leader.wait(10)
    try:
        monkeypatch.setattr(dev, "stop_tree", lambda pid, **kw: None)
        dev._signal(leader, force=True)
        assert len(dev._CLEANUP_FAILURES) == before, "control: a tree stop that reached the tree is no failure"
        monkeypatch.setattr(dev, "stop_tree", lambda pid, **kw: "taskkill.exe not found under the system root")
        dev._signal(leader, force=True)
        assert len(dev._CLEANUP_FAILURES) == before + 1
        assert "taskkill.exe not found" in dev._CLEANUP_FAILURES[-1]
        assert dev.exit_status(0, dev._CLEANUP_FAILURES) == 1
    finally:
        del dev._CLEANUP_FAILURES[before:]


def test_tools_resolve_only_from_absolute_path_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A relative PATH entry resolves against whatever directory a child starts in, so it is
    never searched, and a relative result is refused (Codex on #6830)."""
    import scripts.electron_dev as dev

    name = "odj-probe-tool.exe" if os.name == "nt" else "odj-probe-tool"
    for where in ("rel", "abs"):
        (tmp_path / where).mkdir()
        tool = tmp_path / where / name
        tool.write_text("")
        tool.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    found = dev._resolve("odj-probe-tool", os.pathsep.join(["rel", str(tmp_path / "abs")]))
    assert found == str(tmp_path / "abs" / name)
    with pytest.raises(SystemExit, match="not on PATH"):
        dev._resolve("odj-probe-tool", "rel")
    assert dev._find("odj-probe-tool", "rel") is None, "the optional lookup (xvfb-run's) is held to the same rule"
    monkeypatch.setattr(dev.shutil, "which", lambda command, path=None: f"rel/{command}")
    with pytest.raises(SystemExit, match="not on PATH"):
        dev._resolve("odj-probe-tool", str(tmp_path / "abs"))
    assert dev._find("odj-probe-tool", str(tmp_path / "abs")) is None


@pytest.mark.skipif(sys.platform == "win32", reason="interrupts the build with SIGALRM, which Windows lacks")
def test_an_interrupted_shell_build_stops_its_whole_tree(tmp_path: Path) -> None:
    """Ctrl+C or SIGTERM while the shell compiles stops pnpm's descendants too (Codex on #6830)."""
    import signal
    import time

    import psutil

    import scripts.electron_dev as dev

    ready = tmp_path / "grandchild.pid"
    build = [
        sys.executable,
        "-c",
        "import pathlib, subprocess, sys, time\n"
        "kid = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"pathlib.Path({str(ready)!r}).write_text(str(kid.pid))\n"
        "time.sleep(120)\n",
    ]

    def interrupt(signum: int, frame: object) -> None:
        if not ready.exists():
            signal.setitimer(signal.ITIMER_REAL, 0.1)
            return
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGALRM, interrupt)
    grandchild: psutil.Process | None = None
    try:
        signal.setitimer(signal.ITIMER_REAL, 0.2)
        with pytest.raises(KeyboardInterrupt):
            dev._build_shell(build, tmp_path, dict(os.environ))
        with contextlib.suppress(psutil.NoSuchProcess):  # already killed and reaped is gone too
            grandchild = psutil.Process(int(ready.read_text()))
        deadline = time.monotonic() + 10
        while grandchild is not None and dev._running(grandchild) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert grandchild is None or not dev._running(grandchild)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if grandchild is not None:
            with contextlib.suppress(psutil.NoSuchProcess):
                grandchild.kill()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups; Windows has none to reuse")
def test_a_group_whose_proof_is_lost_before_the_kill_fails_the_cleanup(tmp_path: Path) -> None:
    """The tracked holder exits on SIGTERM while an untracked member ignores it (Codex on #6830).

    The group can no longer be proven ours, so it is not killed, and that must be a
    cleanup failure: the untracked member may still hold a port.
    """
    import subprocess
    import time

    import psutil

    import scripts.electron_dev as dev

    ready = tmp_path / "members.pid"
    # The holder's parent stays to reap it: an unreaped (zombie) holder still reserves the
    # group id, which rightly keeps the group provably ours. The parent ignores SIGTERM so
    # it outlives the holder, then exits once the holder is reaped.
    parent_src = (
        "import signal, subprocess, sys\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "holder = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], "
        "preexec_fn=lambda: signal.signal(signal.SIGTERM, signal.SIG_DFL))\n"
        "print(holder.pid, flush=True)\n"
        "holder.wait()\n"
    )
    leader_src = (
        "import pathlib, subprocess, sys\n"
        f"parent = subprocess.Popen([sys.executable, '-c', {parent_src!r}], stdout=subprocess.PIPE, text=True)\n"
        "holder = int(parent.stdout.readline())\n"
        "stubborn = subprocess.Popen([sys.executable, '-c', "
        "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)'])\n"
        f"pathlib.Path({str(ready)!r}).write_text(f'{{holder}} {{stubborn.pid}}')\n"
    )
    leader = subprocess.Popen([sys.executable, "-c", leader_src], start_new_session=True)
    deadline = time.monotonic() + 20
    while not ready.exists() or not ready.read_text():
        assert time.monotonic() < deadline, "the leader never started its children"
        time.sleep(0.05)
    holder, stubborn = (psutil.Process(int(pid)) for pid in ready.read_text().split())
    leader.wait(20)
    time.sleep(0.3)  # let the stubborn member install its SIGTERM handler
    before = len(dev._CLEANUP_FAILURES)
    try:
        dev._REMEMBERED[leader.pid] = {holder.pid: holder}
        dev._stop(leader, "test leader", grace_s=1.0, groups=True)
        assert not dev._running(holder), "control: the holder did take the SIGTERM"
        assert dev._running(stubborn), "control: the untracked member really survived"
        assert len(dev._CLEANUP_FAILURES) == before + 1
        assert "was not killed" in dev._CLEANUP_FAILURES[-1]
    finally:
        del dev._CLEANUP_FAILURES[before:]
        dev._REMEMBERED.pop(leader.pid, None)
        for member in (holder, stubborn):
            with contextlib.suppress(psutil.NoSuchProcess):
                member.kill()
                member.wait(10)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups; Windows has none to look in")
def test_a_member_started_after_the_snapshot_of_a_reaped_leader_fails_the_cleanup(tmp_path: Path) -> None:
    """The leader starts a child after the last `remember_tree` and exits before the next (Sol P1 on #6830).

    Nothing tracked holds the group, so it is not signalled; a member left in it must
    still fail the cleanup rather than let the stop pass as clean.
    """
    import subprocess
    import time

    import psutil

    import scripts.electron_dev as dev

    def leader_with_child() -> tuple[subprocess.Popen[bytes], psutil.Process]:
        ready = tmp_path / f"child-{time.monotonic_ns()}.pid"
        src = (
            "import pathlib, subprocess, sys\n"
            "kid = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
            f"pathlib.Path({str(ready)!r}).write_text(str(kid.pid))\n"
        )
        leader = subprocess.Popen([sys.executable, "-c", src], start_new_session=True)
        leader.wait(20)
        return leader, psutil.Process(int(ready.read_text()))

    before = len(dev._CLEANUP_FAILURES)
    children: list[psutil.Process] = []
    try:
        leader, child = leader_with_child()
        children.append(child)
        dev._stop(leader, "test leader", grace_s=0.5, groups=True)
        assert dev._running(child), "control: the unproven group was not signalled"
        assert len(dev._CLEANUP_FAILURES) == before + 1
        assert "still has members nothing tracked" in dev._CLEANUP_FAILURES[-1]

        # Control: the same tree, remembered before the leader exited, is stopped cleanly.
        leader, child = leader_with_child()
        children.append(child)
        dev._REMEMBERED[leader.pid] = {child.pid: child}
        dev._stop(leader, "test leader", grace_s=5.0, groups=True)
        assert not dev._running(child)
        assert len(dev._CLEANUP_FAILURES) == before + 1, "control: a tracked child is no failure"

        # Control: a reaped leader that left nothing behind is no failure either.
        bare = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        bare.wait(20)
        dev._stop(bare, "test leader", grace_s=0.5, groups=True)
        assert len(dev._CLEANUP_FAILURES) == before + 1
    finally:
        del dev._CLEANUP_FAILURES[before:]
        for child in children:
            with contextlib.suppress(psutil.NoSuchProcess):
                child.kill()
                child.wait(10)


def test_the_fixture_builder_runs_through_the_setup_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fixture build is stopped as a tree on Ctrl+C or SIGTERM, like the shell build (Codex on #6830)."""
    import scripts.electron_dev as dev

    calls: list[tuple[str, str]] = []
    codes = iter([0, 3])

    def fake(argv: list[str], what: str, *, service: str, cwd: Path, env: dict[str, str]) -> int:
        calls.append((what, service))
        return next(codes)

    monkeypatch.setattr(dev, "_run_setup", fake)
    data_dir = dev._fixture_data_dir(5173, {})
    assert data_dir.name == "fixture-library"
    assert calls == [("fixture builder", "fixture-build")]
    with pytest.raises(SystemExit, match="fixture builder exited 3"):
        dev._fixture_data_dir(5173, {})


def test_setup_children_get_the_claimed_ports_not_the_pair_just_exported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fixture builder and the shell build see the ports claim_ports() returned (Sol P1 on #6830)."""
    import argparse

    import scripts.electron_dev as dev
    from apps.webui.port_config import BACKEND_ENV, FRONTEND_ENV

    monkeypatch.setenv(BACKEND_ENV, "1111")
    monkeypatch.setenv(FRONTEND_ENV, "2222")
    seen: list[dict[str, str]] = []

    def fake(argv: list[str], what: str, *, service: str, cwd: Path, env: dict[str, str]) -> int:
        seen.append(env)
        return 0

    monkeypatch.setattr(dev, "_run_setup", fake)
    args = argparse.Namespace(fixture=True, data_dir=None)
    monkeypatch.setenv("MDT_LIBRARY_MODE", "local")
    dev._backend_env(args, backend_port=8400, frontend_port=5400, backend_up=False)
    assert [(env[BACKEND_ENV], env[FRONTEND_ENV]) for env in seen] == [("8400", "5400")]

    # The shell build, through a real child: what it sees is what it was handed.
    monkeypatch.undo()
    monkeypatch.setenv(BACKEND_ENV, "1111")
    out = tmp_path / "seen.txt"
    build = [
        sys.executable,
        "-c",
        f"import os, pathlib; pathlib.Path({str(out)!r}).write_text(os.environ[{BACKEND_ENV!r}])",
    ]
    assert dev._build_shell(build, tmp_path, dev.with_ports(dict(os.environ), backend=8400, frontend=5400))
    assert out.read_text() == "8400"
    assert os.environ[BACKEND_ENV] == "1111", "control: the stale pair really was in this process's environment"
