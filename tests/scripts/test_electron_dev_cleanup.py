"""The electron-dev launch plan: isolation from the installed app, per-worktree
ports and profiles, and headless display selection (scripts/electron_dev.py)."""

from __future__ import annotations

import contextlib
import os
import socket
import sys
import threading
from pathlib import Path

import pytest

from scripts.electron_dev import (
    _answers,
)
from tests.scripts.electron_dev_helpers import (
    _WINDOW_SRC,
    _plan,
    _until,
)


def test_a_leader_dying_during_startup_still_has_its_descendants_stopped(tmp_path: Path) -> None:
    """A leader that exits inside _wait_for leaves its helpers to _stop, not orphaned (Codex P2 on #6830)."""
    import subprocess
    import sys

    import psutil

    import scripts.electron_dev as dev

    ready = tmp_path / "child.pid"
    leader_src = (
        "import pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"pathlib.Path({str(ready)!r}).write_text(str(child.pid))\n"
        "time.sleep(2)\n"
    )
    leader = subprocess.Popen([sys.executable, "-c", leader_src])
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]  # nothing listens here, so the leader exits first
    grandchild: psutil.Process | None = None
    try:
        with pytest.raises(SystemExit, match="exited with 0 before answering"):
            dev._wait_for(f"http://127.0.0.1:{port}/", "leader", leader)
        grandchild = psutil.Process(int(ready.read_text()))
        assert grandchild.is_running()
        dev._stop(leader, "test leader", grace_s=5.0, groups=False)
        grandchild.wait(10)
        assert not grandchild.is_running()
    finally:
        if grandchild is not None:
            with contextlib.suppress(psutil.NoSuchProcess):
                grandchild.kill()
        dev._REMEMBERED.pop(leader.pid, None)


def test_a_reaped_leaders_reused_pid_is_not_walked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Once poll() has reaped the leader, its pid may name an unrelated process (Codex P2 on #6830).

    `_tree` is pointed at a real stranger to stand in for that reuse: neither
    `remember_tree` nor `_tracked` may pick it up, and a remembered entry survives.
    """
    import subprocess
    import sys

    import psutil

    import scripts.electron_dev as dev

    leader = subprocess.Popen([sys.executable, "-c", "pass"])
    leader.wait(10)
    remembered = psutil.Process(os.getpid())
    stranger = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        dev._REMEMBERED[leader.pid] = {remembered.pid: remembered}
        monkeypatch.setattr(dev, "_tree", lambda pid: [psutil.Process(stranger.pid)])
        dev.remember_tree(leader)
        assert [p.pid for p in dev._tracked(leader)] == [remembered.pid]
    finally:
        dev._REMEMBERED.pop(leader.pid, None)
        stranger.kill()
        stranger.wait(10)


def test_a_running_leaders_tree_is_still_refreshed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control for the reuse guard: while the leader is unreaped its pid is ours, so its tree is
    walked, and a remembered entry still wins over a fresh one with the same pid."""
    import subprocess
    import sys

    import psutil

    import scripts.electron_dev as dev

    leader = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        seen = psutil.Process(leader.pid)
        dev._REMEMBERED[leader.pid] = {leader.pid: seen}
        fresh = psutil.Process(os.getpid())
        monkeypatch.setattr(dev, "_tree", lambda pid: [psutil.Process(pid), fresh])
        tracked = dev._tracked(leader)
        assert {p.pid for p in tracked} == {leader.pid, fresh.pid}
        assert next(p for p in tracked if p.pid == leader.pid) is seen
    finally:
        dev._REMEMBERED.pop(leader.pid, None)
        leader.kill()
        leader.wait(10)


def test_a_failed_shell_rebuild_stops_the_window_until_the_source_compiles(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Agents must not drive the previous build over CDP after an edit stops compiling (Sol P1 on #6830).

    Real processes and real files throughout (Codex P1 on #6830): the window is a
    process serving /json/version, the build is a real compiler run over the
    disposable source, and the loop watches that source by mtime.
    """
    import dataclasses
    import json
    import subprocess
    import sys
    import urllib.request

    import scripts.electron_dev as dev

    shell = tmp_path / "shell"
    (shell / "src").mkdir(parents=True)
    source = shell / "src" / "main.ts"
    source.write_text("x = 1\n")
    build = [
        sys.executable,
        "-c",
        "import pathlib; compile(pathlib.Path('src/main.ts').read_text(), 'main.ts', 'exec')",
    ]
    close = tmp_path / "close"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    cdp = f"http://127.0.0.1:{port}"
    plan = dataclasses.replace(
        _plan(tmp_path), argv=[sys.executable, "-c", _WINDOW_SRC, str(port), str(close)], env=dict(os.environ)
    )

    def edit(text: str, bump_s: float) -> None:
        source.write_text(text)
        stat = source.stat()
        os.utime(source, (stat.st_atime, stat.st_mtime + bump_s))  # never the same mtime twice

    def serving_pid() -> int | None:
        try:
            with urllib.request.urlopen(f"{cdp}/json/version", timeout=2) as reply:
                return int(json.load(reply)["pid"])
        except OSError:
            return None

    first = dev._spawn(plan.argv, env=plan.env, service="electron")
    windows: list[subprocess.Popen[bytes]] = [first]
    result: list[int] = []
    try:
        dev._wait_for(f"{cdp}/json/version", "window", first)
        # Taken here, as main() takes it before the first build: an edit saved
        # before the watcher thread first runs must still count (seen on CI).
        stamp = dev.shell_sources_stamp(shell / "src")
        watcher = threading.Thread(
            target=lambda: result.append(
                dev._watch_shell(
                    windows,
                    plan,
                    cdp=cdp,
                    shell=dev.ShellBuild(build, shell, stamp, dict(os.environ)),
                    no_watch=False,
                    started=[],
                )
            ),
            daemon=True,
        )
        watcher.start()

        edit("x = (\n", 1)
        _until(lambda: first.poll() is not None, "the failed build to stop the window")
        # Stopped and dropped, so the cleanup cannot signal its reaped pid again,
        # and the stopped window does not end the run.
        assert windows == []
        assert watcher.is_alive() and not result

        edit("x = 2\n", 2)
        out: list[str] = []

        def relaunched() -> bool:
            out.append(capsys.readouterr().out)
            return "[OK] relaunched" in "".join(out)

        # The loop's own readiness wait has seen the new window, not just this test.
        _until(relaunched, "the good build to relaunch the window")
        assert windows[0] is not first and serving_pid() == windows[0].pid

        close.write_text("closed")
        watcher.join(30)
        assert result == [0]
        out.append(capsys.readouterr().out)
        assert "".join(out).count("[ERROR] window stopped until the shell compiles") == 1
    finally:
        close.write_text("closed")
        for window in [first, *windows]:
            dev._stop(window, "test window", grace_s=5.0)


@pytest.mark.parametrize("status", [401, 404, 500, 503])
def test_a_health_endpoint_that_answers_an_error_is_not_alive(status_origin: str, status: int) -> None:
    """Sol on #6830: a missing or rejected /api/v1/health must never read as a live backend."""
    assert _answers(f"{status_origin}/{status}") is False


@pytest.mark.parametrize("path", ["/200", "/204", "/moved"])
def test_a_success_answer_is_alive_even_after_a_redirect(status_origin: str, path: str) -> None:
    """Control: the success the probe exists to see still reads as alive."""
    assert _answers(f"{status_origin}{path}") is True


def test_nothing_listening_is_not_alive() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert _answers(f"http://127.0.0.1:{port}/") is False


def test_a_stop_during_spawn_lands_only_after_the_child_is_registered() -> None:
    """A SIGTERM between creating a child and recording it would leave cleanup without a
    handle on it; it is held until `register` has run, then delivered (Sol P1 on #6830)."""
    import signal
    import subprocess

    import scripts.electron_dev as dev

    registered: list[subprocess.Popen[bytes]] = []
    spawned: list[subprocess.Popen[bytes]] = []

    def register(process: subprocess.Popen[bytes]) -> None:
        spawned.append(process)
        signal.raise_signal(signal.SIGTERM)  # the stop arrives before the caller records it
        registered.append(process)

    previous = signal.signal(signal.SIGTERM, dev._terminate)
    try:
        with pytest.raises(dev.Terminated):
            dev._spawn([sys.executable, "-c", "pass"], service="test", register=register)
        assert registered == spawned, "the stop cut registration short"
        assert signal.getsignal(signal.SIGTERM) is dev._terminate, "the handler was not restored"
    finally:
        signal.signal(signal.SIGTERM, previous)
        for process in spawned:
            process.wait(20)


def test_a_ctrl_c_during_spawn_is_replayed_and_a_quiet_spawn_changes_nothing() -> None:
    import signal
    import subprocess

    import scripts.electron_dev as dev

    registered: list[subprocess.Popen[bytes]] = []

    def register(process: subprocess.Popen[bytes]) -> None:
        signal.raise_signal(signal.SIGINT)
        registered.append(process)

    before = signal.getsignal(signal.SIGINT)
    try:
        with pytest.raises(KeyboardInterrupt):
            dev._spawn([sys.executable, "-c", "pass"], service="test", register=register)
        assert len(registered) == 1
        assert signal.getsignal(signal.SIGINT) is before
        # Control: with no stop, the child starts with SIGTERM at its default (deferring
        # swaps Python handlers, it never ignores or blocks), and no stop is invented.
        quiet = dev._spawn(
            [
                sys.executable,
                "-c",
                "import signal, sys; sys.exit(0 if signal.getsignal(signal.SIGTERM) == signal.SIG_DFL else 3)",
            ],
            service="test",
            register=registered.append,
        )
        assert quiet.wait(20) == 0
        assert registered[-1] is quiet
    finally:
        for process in registered:
            process.wait(20)


@pytest.mark.skipif(sys.platform == "win32", reason="an unreaped exited child shows as a zombie only on POSIX")
def test_waiting_for_a_tree_never_opens_a_pidfd(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] `_stop` waits on its tracked processes [then] it polls them, never `psutil.wait_procs`,
    whose pidfd_open answers EINVAL for a process caught mid-reap (seen on CI for #6830), [else stop]."""
    import subprocess

    import psutil

    import scripts.electron_dev as dev

    def refuse(*_: object, **__: object) -> None:
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(psutil, "wait_procs", refuse)
    running = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    exited = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        zombie = psutil.Process(exited.pid)
        assert dev._wait_gone([zombie], 20) == [], "an exited child is gone once it is a zombie"
        assert exited.returncode is None, "the reap is left to Popen, which keeps the exit code"
        still = psutil.Process(running.pid)
        assert dev._wait_gone([still], 0.2) == [still], "control: a running process is still there"
    finally:
        running.kill()
        running.wait(10)
        exited.wait(10)
