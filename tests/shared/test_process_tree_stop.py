"""The cross-platform tree stop in apps/shared/process_groups.py: process groups
on POSIX, taskkill /T on Windows, both arms behind one seam (AGENTS.md
"Portable by default"; scripts/electron_dev.py is the caller)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from apps.shared.process_groups import (
    GROUPS_SUPPORTED,
    group_alive,
    group_gone_within,
    group_held_by,
    stop_tree,
    tree_kill_argv,
    windows_taskkill,
)

TASKKILL = "C:/Windows/System32/taskkill.exe"


def test_windows_stops_the_whole_tree_with_taskkill() -> None:
    # terminate() reaches only the direct child on Windows.
    argv = tree_kill_argv(4242, platform="win32", taskkill=TASKKILL)
    assert argv == [TASKKILL, "/T", "/F", "/PID", "4242"]


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_posix_leaves_tree_stopping_to_process_groups(platform: str) -> None:
    assert tree_kill_argv(4242, platform=platform, taskkill="/bin/taskkill") is None


def test_taskkill_is_resolved_under_the_system_root_never_on_path() -> None:
    """Codex on #6830: a sanitized or shadowing PATH must not decide which taskkill runs."""
    seen: list[str] = []

    def present(path: str) -> bool:
        seen.append(path)
        return True

    assert windows_taskkill({"SystemRoot": "C:\\Windows", "PATH": "C:\\evil"}, exists=present) == (
        "C:\\Windows\\System32\\taskkill.exe"
    )
    assert windows_taskkill({"windir": "D:\\WINNT"}, exists=present) == "D:\\WINNT\\System32\\taskkill.exe"
    assert seen == ["C:\\Windows\\System32\\taskkill.exe", "D:\\WINNT\\System32\\taskkill.exe"]


@pytest.mark.parametrize(
    ("env", "exists"),
    [
        ({"PATH": "C:\\Windows\\System32"}, True),  # no system root at all
        ({"SystemRoot": "Windows"}, True),  # a relative root
        ({"SystemRoot": "C:\\Windows"}, False),  # no taskkill.exe there
    ],
)
def test_no_taskkill_under_an_absolute_system_root_is_none(env: dict[str, str], exists: bool) -> None:
    assert windows_taskkill(env, exists=lambda _path: exists) is None


def test_windows_without_taskkill_kills_the_leader_and_warns_instead_of_aborting() -> None:
    """The caller's cleanup goes on (it judges by which tracked processes survive)."""
    leader = subprocess.Popen([sys.executable, "-c", _SLEEPER])
    try:
        warning = stop_tree(
            leader.pid, force=False, kill_leader=leader.kill, groups=False, platform="win32", taskkill=lambda: None
        )
        assert warning is not None
        assert "taskkill.exe not found" in warning
        assert leader.wait(10) is not None
    finally:
        _reap(leader, None)


_SLEEPER = "import time\nwhile True: time.sleep(0.05)\n"


def _leader_with_child(tmp_path: Path, *, new_session: bool) -> tuple[subprocess.Popen[bytes], psutil.Process]:
    """A real leader that spawned a real child, once both are running."""
    ready = tmp_path / "child.pid"
    leader_src = (
        "import pathlib, subprocess, sys, time\n"
        f"child = subprocess.Popen([sys.executable, '-c', {_SLEEPER!r}])\n"
        f"pathlib.Path({str(ready)!r}).write_text(str(child.pid))\n"
        "while True: time.sleep(0.05)\n"
    )
    leader = subprocess.Popen([sys.executable, "-c", leader_src], start_new_session=new_session)
    deadline = time.monotonic() + 20
    while not ready.exists() or not ready.read_text():
        if time.monotonic() >= deadline:
            leader.kill()
            pytest.fail("the leader never started its child")
        time.sleep(0.05)
    return leader, psutil.Process(int(ready.read_text()))


def _reap(leader: subprocess.Popen[bytes], child: psutil.Process | None) -> None:
    if leader.poll() is None:
        leader.kill()
        leader.wait(10)
    if child is not None and child.is_running():
        child.kill()


WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="Windows arm: needs taskkill.exe")


@WINDOWS_ONLY
def test_the_windows_arm_stops_the_leader_and_what_it_spawned(tmp_path: Path) -> None:
    leader, child = _leader_with_child(tmp_path, new_session=False)
    try:
        assert stop_tree(leader.pid, force=False, kill_leader=leader.kill) is None
        leader.wait(10)
        child.wait(10)
        assert not child.is_running(), "taskkill /T must reach the child, not only the leader"
    finally:
        _reap(leader, child)


@WINDOWS_ONLY
def test_a_failed_taskkill_is_reported_not_swallowed() -> None:
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait(10)
    warning = stop_tree(gone.pid, force=False, kill_leader=gone.kill)
    assert warning is not None
    assert warning.startswith("taskkill exited ")


def test_a_platform_with_neither_mechanism_kills_the_leader(tmp_path: Path) -> None:
    leader = subprocess.Popen([sys.executable, "-c", _SLEEPER])
    try:
        assert stop_tree(leader.pid, force=True, kill_leader=leader.kill, groups=False, platform="plan9") is None
        assert leader.wait(10) is not None
    finally:
        _reap(leader, None)


@pytest.mark.skipif(not GROUPS_SUPPORTED, reason="POSIX arm: needs os.killpg process groups")
@pytest.mark.parametrize("force", [False, True])
def test_the_posix_arm_stops_the_leader_and_what_it_spawned(tmp_path: Path, force: bool) -> None:
    leader, child = _leader_with_child(tmp_path, new_session=True)
    try:
        assert group_alive(leader.pid) is True
        # Control: the same live group reads False only because groups=False says none exist.
        assert group_alive(leader.pid, groups=False) is False

        assert stop_tree(leader.pid, force=force, kill_leader=leader.kill) is None

        leader.wait(10)
        child.wait(10)
        assert not child.is_running()
        assert group_alive(leader.pid) is False
    finally:
        _reap(leader, child)


def test_without_process_groups_no_group_is_ever_held(tmp_path: Path) -> None:
    """The non-POSIX arm of group_held_by: with no groups there is nothing to prove, on any platform."""
    leader, child = _leader_with_child(tmp_path, new_session=GROUPS_SUPPORTED)
    try:
        assert group_held_by(leader.pid, [child], groups=False) is False
    finally:
        _reap(leader, child)


@pytest.mark.skipif(not GROUPS_SUPPORTED, reason="POSIX arm: needs os.getpgid process groups")
def test_a_group_is_held_only_by_a_running_member_of_it(tmp_path: Path) -> None:
    """The POSIX arm: a live member holds its group; a process in another group, or a dead one, does not."""
    leader, child = _leader_with_child(tmp_path, new_session=True)
    try:
        assert group_held_by(leader.pid, [child]) is True
        # The test runner is in a different group, so it holds nothing here.
        assert group_held_by(leader.pid, [psutil.Process()]) is False
        assert group_held_by(leader.pid, []) is False
        # A process that has exited and been reaped holds nothing, even though its
        # psutil handle was taken while it ran.
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        handle = psutil.Process(gone.pid)
        gone.wait(10)
        assert group_held_by(os.getpgid(os.getpid()), [handle]) is False
    finally:
        _reap(leader, child)


def test_without_process_groups_a_group_is_always_gone() -> None:
    assert group_gone_within(4242, 0.0, groups=False) is True


@pytest.mark.skipif(not GROUPS_SUPPORTED, reason="POSIX arm: needs os.killpg process groups")
def test_a_group_whose_last_member_is_still_exiting_is_waited_for_not_reported(tmp_path: Path) -> None:
    """Codex on #6830: a member that is on its way out is given the grace, not judged at once."""
    leader = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.6)"], start_new_session=True)
    try:
        assert group_alive(leader.pid) is True  # an instant check would call it a survivor
        assert group_gone_within(leader.pid, 10.0) is True
    finally:
        _reap(leader, None)


@pytest.mark.skipif(not GROUPS_SUPPORTED, reason="POSIX arm: needs os.killpg process groups")
def test_a_group_that_outlives_the_grace_is_reported(tmp_path: Path) -> None:
    """Control: the wait ends, and a member that is really still there is still a survivor."""
    leader, child = _leader_with_child(tmp_path, new_session=True)
    try:
        assert group_gone_within(leader.pid, 0.3) is False
    finally:
        _reap(leader, child)
