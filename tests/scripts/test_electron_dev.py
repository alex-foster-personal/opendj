"""The electron-dev launch plan: isolation from the installed app, per-worktree
ports and profiles, and headless display selection (scripts/electron_dev.py)."""

from __future__ import annotations

import contextlib
import http.server
import os
import socket
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from scripts.electron_dev import (
    CDP_PORT_OFFSET,
    ELECTRON_DIR,
    REPO_ROOT,
    _answers,
    _port_free,
    backend_reuse_refusal,
    needs_virtual_display,
    plan_launch,
)


def _plan(
    tmp_path: Path,
    *,
    frontend_port: int = 9400,
    platform: str = "darwin",
    environ: dict[str, str] | None = None,
    is_root: bool = False,
    xvfb_run: str | None = None,
):
    return plan_launch(
        electron_binary="/opt/electron",
        backend_port=8400,
        frontend_port=frontend_port,
        environ={"PATH": "/usr/bin"} if environ is None else environ,
        platform=platform,
        is_root=is_root,
        xvfb_run=xvfb_run,
        tmp_root=tmp_path,
    )


def test_the_window_gets_its_own_profile_and_shell_data_dir(tmp_path: Path) -> None:
    # R-1: without --user-data-dir the dev window takes the installed app's
    # single-instance lock and exits; without OPENDJ_SHELL_DATA_DIR it logs
    # into that app's library.
    plan = _plan(tmp_path, environ={"OPENDJ_SHELL_DATA_DIR": "/inherited/installed-app-data"})
    profile = [a for a in plan.argv if a.startswith("--user-data-dir=")]
    assert plan.state_dir.parent == tmp_path
    assert plan.state_dir.name.startswith("opendj-electron-dev-9400-")
    assert profile == [f"--user-data-dir={plan.state_dir / 'user-data'}"]
    assert plan.env["OPENDJ_SHELL_DATA_DIR"] == str(plan.state_dir / "shell-data")
    assert plan.argv[-1] == str(ELECTRON_DIR)


def test_the_window_opens_on_the_vite_origin_with_a_cdp_port(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    assert plan.env["OPENDJ_ENGINE_ORIGIN"] == "http://127.0.0.1:9400"
    assert plan.env["OPENDJ_REMOTE_DEBUGGING_PORT"] == str(9400 + CDP_PORT_OFFSET)
    assert plan.cdp_port == 9400 + CDP_PORT_OFFSET


def test_the_window_gets_the_claimed_ports_not_the_inherited_pair(tmp_path: Path) -> None:
    """Just exports `.env` before claim_ports() runs (Sol P1 on #6830)."""
    from apps.webui.port_config import BACKEND_ENV, FRONTEND_ENV

    plan = _plan(tmp_path, environ={BACKEND_ENV: "1111", FRONTEND_ENV: "2222", "PATH": "/usr/bin"})
    assert plan.env[BACKEND_ENV] == "8400"
    assert plan.env[FRONTEND_ENV] == "9400"
    assert plan.env["PATH"] == "/usr/bin"


def test_two_worktrees_never_share_a_port_or_profile(tmp_path: Path) -> None:
    # R-2
    first, second = _plan(tmp_path, frontend_port=9400), _plan(tmp_path, frontend_port=9401)
    assert first.cdp_port != second.cdp_port
    assert first.state_dir != second.state_dir


def test_two_worktrees_given_the_same_port_never_share_a_profile(tmp_path: Path) -> None:
    """A released port is later claimed by another worktree (Sol P1 on #6830)."""
    from scripts.electron_dev import state_dir_for

    one = state_dir_for(9400, tmp_path, worktree=tmp_path / "wt-one")
    two = state_dir_for(9400, tmp_path, worktree=tmp_path / "wt-two")
    assert one != two
    # Control: the same worktree keeps the same profile across runs.
    assert state_dir_for(9400, tmp_path, worktree=tmp_path / "wt-one") == one


@pytest.mark.parametrize(
    ("platform", "environ", "expected"),
    [
        ("linux", {}, True),
        ("linux", {"DISPLAY": ":0"}, False),
        ("linux", {"WAYLAND_DISPLAY": "wayland-0"}, False),
        ("darwin", {}, False),
        ("win32", {}, False),
    ],
)
def test_only_linux_without_a_display_needs_xvfb(platform: str, environ: dict[str, str], expected: bool) -> None:
    # R-3
    assert needs_virtual_display(platform, environ) is expected


def test_headless_linux_runs_the_window_under_xvfb_run(tmp_path: Path) -> None:
    plan = _plan(tmp_path, platform="linux", environ={}, xvfb_run="/usr/bin/xvfb-run")
    assert plan.argv[:3] == ["/usr/bin/xvfb-run", "-a", "-s"]
    assert plan.argv[4] == "/opt/electron"


def test_headless_linux_without_xvfb_run_refuses_loudly(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="xvfb-run"):
        _plan(tmp_path, platform="linux", environ={}, xvfb_run=None)


def test_a_desktop_session_never_gets_xvfb(tmp_path: Path) -> None:
    plan = _plan(tmp_path, platform="darwin", xvfb_run="/usr/bin/xvfb-run")
    assert plan.argv[0] == "/opt/electron"


@pytest.mark.parametrize(("is_root", "expected"), [(True, True), (False, False)])
def test_only_root_turns_the_sandbox_off(tmp_path: Path, is_root: bool, expected: bool) -> None:
    # R-4
    assert ("--no-sandbox" in _plan(tmp_path, is_root=is_root).argv) is expected


@pytest.mark.parametrize(
    ("backend_up", "explicit_source", "refused"),
    [(True, True, True), (True, False, False), (False, True, False), (False, False, False)],
)
def test_an_explicit_library_never_reuses_a_running_backend(
    backend_up: bool, explicit_source: bool, refused: bool
) -> None:
    # R-5: a backend already up serves its own library, maybe a real one.
    reason = backend_reuse_refusal(
        backend_up=backend_up, explicit_source=explicit_source, origin="http://127.0.0.1:8680"
    )
    assert (reason is not None) is refused


def test_the_highest_frontend_port_with_a_legal_cdp_port_is_accepted(tmp_path: Path) -> None:
    assert _plan(tmp_path, frontend_port=65535 - CDP_PORT_OFFSET).cdp_port == 65535


def test_a_frontend_port_with_no_legal_cdp_port_refuses_loudly(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="no CDP port"):
        _plan(tmp_path, frontend_port=65536 - CDP_PORT_OFFSET)


def test_source_stamp_changes_when_an_older_file_is_deleted(tmp_path: Path) -> None:
    from scripts.electron_dev import shell_sources_stamp

    older, newer = tmp_path / "a.ts", tmp_path / "b.ts"
    older.write_text("a")
    newer.write_text("b")

    os.utime(older, (1_000, 1_000))
    os.utime(newer, (2_000, 2_000))
    before = shell_sources_stamp(tmp_path)
    older.unlink()
    assert shell_sources_stamp(tmp_path) != before, "if deleting a non-newest source keeps the stamp then broken"


def test_source_stamp_is_stable_when_nothing_changes(tmp_path: Path) -> None:
    from scripts.electron_dev import shell_sources_stamp

    (tmp_path / "a.ts").write_text("a")
    assert shell_sources_stamp(tmp_path) == shell_sources_stamp(tmp_path)


@pytest.mark.parametrize(("returncode", "expected"), [(0, 0), (1, 1), (-11, 1), (139, 1)])
def test_window_exit_status_reports_a_crash(returncode: int, expected: int) -> None:
    from scripts.electron_dev import window_exit_status

    assert window_exit_status(returncode) == expected


def test_child_env_stamps_its_own_service_id_over_an_inherited_one() -> None:
    from scripts.electron_dev import child_env

    env = child_env({"PATH": "/usr/bin", "AF_SERVICE_ID": "com.other.thing"}, "vite", "com.opendj")
    assert env["AF_SERVICE_ID"] == "com.opendj.dev.electron-dev.vite"
    assert env["PATH"] == "/usr/bin"
    assert ".test." not in env["AF_SERVICE_ID"], "a dev server must not be reapable as a test leftover"


def test_source_stamp_skips_a_file_gone_between_listing_and_stat(tmp_path: Path) -> None:
    from scripts.electron_dev import shell_sources_stamp

    kept = tmp_path / "a.ts"
    kept.write_text("a")
    # A dangling link is listed by rglob but its stat raises FileNotFoundError: the
    # same real state an atomic save leaves when the file vanishes after listing.
    gone = tmp_path / "b.ts"
    try:
        gone.symlink_to(tmp_path / "deleted.ts")
    except OSError as error:  # Windows without the symlink privilege
        pytest.skip(f"creating a symlink is not permitted here: {error}")
    assert gone in set(tmp_path.rglob("*.ts")), "the fixture must be listed, or this test proves nothing"
    stamp = shell_sources_stamp(tmp_path)
    assert {name for name, _ in stamp} == {str(kept)}, "if a vanished file crashes or stays in the stamp then broken"


def test_survivors_are_named_and_none_means_clean() -> None:
    from scripts.electron_dev import survivors_message

    assert survivors_message("vite", []) is None
    message = survivors_message("vite", [42, 7])
    assert message is not None
    assert message.startswith("[ERROR]")
    assert "7, 42" in message


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups are POSIX-only; Windows uses taskkill /T")
def test_stop_kills_a_descendant_that_ignores_sigterm_after_its_leader_exits() -> None:
    import signal
    import subprocess
    import sys

    import psutil

    from scripts import electron_dev

    # The leader exits on SIGTERM; its child ignores SIGTERM and would live on.
    child = (
        "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('up', flush=True); time.sleep(60)"
    )
    leader = (
        "import subprocess, sys, time; "
        f"p = subprocess.Popen([sys.executable, '-c', {child!r}], stdout=subprocess.PIPE); "
        "p.stdout.readline(); print(p.pid, flush=True); time.sleep(60)"
    )
    process = subprocess.Popen([sys.executable, "-c", leader], start_new_session=True, stdout=subprocess.PIPE)
    assert process.stdout is not None
    grandchild = int(process.stdout.readline())
    failures_before = len(electron_dev._CLEANUP_FAILURES)
    try:
        electron_dev._stop(process, "probe", grace_s=1.0)
        assert not psutil.pid_exists(grandchild) or psutil.Process(grandchild).status() == psutil.STATUS_ZOMBIE
        assert len(electron_dev._CLEANUP_FAILURES) == failures_before
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)


@pytest.mark.parametrize(
    ("main_status", "failures", "expected"), [(0, [], 0), (0, ["x"], 1), (2, ["x"], 2), (3, [], 3)]
)
def test_a_failed_cleanup_makes_a_clean_exit_fail(main_status: int, failures: list[str], expected: int) -> None:
    from scripts.electron_dev import exit_status

    assert exit_status(main_status, failures) == expected


_RUN_BODY = (
    "import pathlib, sys, time\n"
    "from scripts.electron_dev import run\n"
    "def body():\n"
    "    try:\n"
    "        if sys.argv[2] == 'ctrl-c':\n"
    "            raise KeyboardInterrupt\n"
    "        print('up', flush=True)\n"
    "        time.sleep(60)\n"
    "        return 0\n"
    "    except KeyboardInterrupt:\n"
    "        return 0\n"
    "    finally:\n"
    "        pathlib.Path(sys.argv[1]).write_text('cleaned')\n"
    "raise SystemExit(run(body))\n"
)


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX signal delivery; Windows has no catchable SIGTERM from os.kill"
)
def test_sigterm_cleans_up_and_exits_143_not_0(tmp_path: Path) -> None:
    """Sol P1 on #6830: a CI timeout or service-manager SIGTERM must not read as success."""
    import signal
    import subprocess

    marker = tmp_path / "cleaned"
    process = subprocess.Popen(
        [sys.executable, "-c", _RUN_BODY, str(marker), "sigterm"],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline() == b"up\n"
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=30) == 143
    finally:
        if process.poll() is None:
            process.kill()
    assert marker.read_text() == "cleaned"


def test_ctrl_c_still_exits_0(tmp_path: Path) -> None:
    """Control for the SIGTERM fix: a user's Ctrl+C ending the session stays a clean exit."""
    import subprocess

    marker = tmp_path / "cleaned"
    done = subprocess.run(
        [sys.executable, "-c", _RUN_BODY, str(marker), "ctrl-c"],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        timeout=60,
        check=False,
    )
    assert done.returncode == 0
    assert marker.read_text() == "cleaned"


def test_a_reused_server_must_serve_this_worktrees_library(tmp_path: Path) -> None:
    from scripts.electron_dev import reuse_mismatch

    ours = tmp_path / "data" / "state" / "state.db"
    assert reuse_mismatch("backend", "http://127.0.0.1:1", str(ours), ours) is None
    other = reuse_mismatch("backend", "http://127.0.0.1:1", str(tmp_path / "other" / "state.db"), ours)
    assert other is not None
    assert "not this worktree's" in other
    silent = reuse_mismatch("Vite server", "http://127.0.0.1:2", None, ours)
    assert silent is not None
    assert "names no state.db" in silent


def test_the_recipe_stamps_the_controller_with_the_project_namespace() -> None:
    from scripts.electron_dev import REPO_ROOT
    from scripts.server_owner_guard import process_namespace

    recipe = (REPO_ROOT / "justfile").read_text(encoding="utf-8")
    expected = f"AF_SERVICE_ID={process_namespace(REPO_ROOT)}.dev.electron-dev.controller uv run"
    assert expected in recipe, "if the electron-dev controller runs unstamped then broken"


@pytest.mark.parametrize(
    ("platform", "current", "explicit", "expected"),
    [
        ("darwin", None, False, None),
        ("linux", "cloud", False, None),
        ("linux", None, True, "local"),
        ("win32", "", True, "local"),
    ],
)
def test_library_mode_is_chosen_only_for_a_named_library(
    platform: str, current: str | None, explicit: bool, expected: str | None
) -> None:
    from scripts.electron_dev import library_mode_for

    assert library_mode_for(platform=platform, current=current, explicit_library=explicit) == expected


def test_an_unset_library_mode_on_the_worktree_library_stops_the_run() -> None:
    from scripts.electron_dev import library_mode_for

    with pytest.raises(SystemExit, match="MDT_LIBRARY_MODE is unset"):
        library_mode_for(platform="linux", current=None, explicit_library=False)


def test_a_port_left_in_time_wait_by_a_closed_shell_counts_as_free() -> None:
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen()
    port = server.getsockname()[1]
    try:
        # Control: a live listener is taken, whatever the probe's socket options.
        assert not _port_free(port)
        client = socket.create_connection(("127.0.0.1", port))
        accepted, _ = server.accept()
        accepted.close()  # the server side closes first, so it holds the TIME_WAIT
        client.close()
    finally:
        server.close()
    assert _port_free(port), "a TIME_WAIT left by the last shell blocked the relaunch"


def test_a_reused_server_must_run_from_this_worktree(tmp_path: Path) -> None:
    """Two worktrees can serve one library, so the library alone does not prove ownership (Sol P1 on #6830)."""
    from scripts.electron_dev import ownership_mismatch

    ours, theirs = tmp_path / "ours", tmp_path / "theirs"
    for root in (ours, theirs):
        (root / "apps" / "webui" / "frontend").mkdir(parents=True)
        (root / ".git").write_text("gitdir: elsewhere\n")
    frontend = ours / "apps" / "webui" / "frontend"
    assert ownership_mismatch("Vite server", "http://127.0.0.1:2", [frontend], ours) is None
    assert ownership_mismatch("backend", "http://127.0.0.1:1", [ours], ours) is None
    other = ownership_mismatch("backend", "http://127.0.0.1:1", [theirs], ours)
    assert other is not None
    assert str(theirs) in other
    # A checkout nested inside ours is not ours either.
    nested = ours / "nested-wt"
    nested.mkdir()
    (nested / ".git").write_text("gitdir: elsewhere\n")
    assert ownership_mismatch("backend", "http://127.0.0.1:1", [nested], ours) is not None
    # Unmeasurable is a refusal, never a pass.
    unknowns: tuple[list[Path] | None, ...] = (None, [])
    for unknown in unknowns:
        refused = ownership_mismatch("backend", "http://127.0.0.1:1", unknown, ours)
        assert refused is not None
        assert "cannot tell which checkout" in refused


def test_a_started_backend_whose_health_stays_down_stops_the_run() -> None:
    """A dead reload worker under a live reloader ends the run past the grace, not before (Sol P1 on #6830)."""
    from scripts.electron_dev import backend_stall

    # Answering clears any outage, so a reload that recovers never stops the run.
    assert backend_stall(True, 100.0, 200.0, grace_s=20.0) == (None, None)
    # The first failed probe starts the clock without stopping anything.
    assert backend_stall(False, None, 100.0, grace_s=20.0) == (100.0, None)
    # Still inside the grace, as during a normal uvicorn worker restart.
    assert backend_stall(False, 100.0, 119.0, grace_s=20.0) == (100.0, None)
    since, message = backend_stall(False, 100.0, 120.5, grace_s=20.0)
    assert since == 100.0
    assert message is not None
    assert "/api/v1/health" in message
    assert "20 s" in message


def test_an_owned_service_that_exits_stops_the_run() -> None:
    """The window must not keep running against a dead backend or Vite (Sol P1 on #6830)."""
    import subprocess
    import sys

    from scripts.electron_dev import dead_service

    alive = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    dead = subprocess.Popen([sys.executable, "-c", "raise SystemExit(3)"])
    try:
        dead.wait(10)
        assert dead_service([(alive, "backend")]) is None
        message = dead_service([(alive, "backend"), (dead, "vite")])
        assert message is not None
        assert "vite" in message
        assert "exited with 3" in message
        assert dead_service([]) is None
    finally:
        alive.kill()
        alive.wait(10)


def test_every_child_gets_the_claimed_ports_not_the_inherited_ones() -> None:
    """Just exports the .env it read before claim_ports() rewrote it (Codex and Sol on #6830)."""
    from scripts.electron_dev import with_ports

    stale = {"MUSIC_DJ_BACKEND_PORT": "8100", "MUSIC_DJ_FRONTEND_PORT": "9100", "KEEP": "1"}
    env = with_ports(stale, backend=8300, frontend=9300)
    assert env["MUSIC_DJ_BACKEND_PORT"] == "8300"
    assert env["MUSIC_DJ_FRONTEND_PORT"] == "9300"
    assert env["KEEP"] == "1"
    assert stale["MUSIC_DJ_BACKEND_PORT"] == "8100"  # the parent's own environment is untouched


def test_a_descendant_outliving_its_reaped_leader_is_still_stopped(tmp_path: Path) -> None:
    """Without process groups, a reaped leader leaves nothing for `_tree()` or `taskkill /T` (Sol on #6830)."""
    import subprocess
    import sys
    import time

    import psutil

    from scripts.electron_dev import _stop, remember_tree

    ready = tmp_path / "ready"
    release = tmp_path / "release"
    leader_src = (
        "import pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"pathlib.Path({str(ready)!r}).write_text(str(child.pid))\n"
        f"while not pathlib.Path({str(release)!r}).exists():\n"
        "    time.sleep(0.05)\n"
    )
    leader = subprocess.Popen([sys.executable, "-c", leader_src])
    grandchild: psutil.Process | None = None
    try:
        deadline = time.monotonic() + 20
        while not ready.exists() or not ready.read_text():
            assert time.monotonic() < deadline, "the leader never started its child"
            time.sleep(0.05)
        grandchild = psutil.Process(int(ready.read_text()))
        remember_tree(leader)
        release.write_text("go")
        leader.wait(20)  # what the watch loop's poll() does: the leader is reaped
        assert grandchild.is_running()

        started = time.monotonic()
        _stop(leader, "test leader", grace_s=5.0, groups=False)
        # Signalled directly with SIGTERM, not left to the kill after the grace.
        assert time.monotonic() - started < 4.0

        grandchild.wait(10)
        assert not grandchild.is_running()
    finally:
        if grandchild is not None:
            with contextlib.suppress(psutil.NoSuchProcess):
                grandchild.kill()


def _group_with_a_reaped_leader(tmp_path: Path):  # type: ignore[no-untyped-def]
    """A real group whose leader has exited and been reaped, with two live members left in it."""
    import subprocess
    import sys
    import time

    import psutil

    ready = tmp_path / "member.pid"
    leader_src = (
        "import pathlib, subprocess, sys\n"
        "kids = [subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']) for _ in range(2)]\n"
        f"pathlib.Path({str(ready)!r}).write_text(' '.join(str(k.pid) for k in kids))\n"
    )
    leader = subprocess.Popen([sys.executable, "-c", leader_src], start_new_session=True)
    deadline = time.monotonic() + 20
    while not ready.exists() or not ready.read_text():
        assert time.monotonic() < deadline, "the leader never started its child"
        time.sleep(0.05)
    members = [psutil.Process(int(pid)) for pid in ready.read_text().split()]
    leader.wait(20)  # reaped: its pid number is now free
    assert all(os.getpgid(member.pid) == leader.pid for member in members)
    return leader, members


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups; Windows has none to reuse")
def test_a_reaped_leaders_group_is_not_signalled_when_nothing_proves_it_ours(tmp_path: Path) -> None:
    """After the leader is reaped, its pid may name someone else's group (Sol P1 on #6830).

    Here the group's only live member is one this run never tracked, which is
    what a reused group id looks like from the inside: it must be left alone.
    """
    import psutil

    from scripts.electron_dev import _REMEMBERED, _stop

    leader, strangers = _group_with_a_reaped_leader(tmp_path)
    try:
        _REMEMBERED.pop(leader.pid, None)
        _stop(leader, "test leader", grace_s=2.0, groups=True)
        for stranger in strangers:
            assert stranger.is_running()
            assert stranger.status() != psutil.STATUS_ZOMBIE
    finally:
        for stranger in strangers:
            with contextlib.suppress(psutil.NoSuchProcess):
                stranger.kill()
                stranger.wait(10)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups; Windows has none to reuse")
def test_a_reaped_leaders_group_is_still_stopped_when_a_tracked_member_holds_it(tmp_path: Path) -> None:
    """Control for the reuse guard: a group a tracked process still holds is ours, so the whole
    group is signalled, reaching a member started after the snapshot that nothing tracked."""
    import psutil

    from scripts.electron_dev import _REMEMBERED, _stop

    leader, (tracked, untracked) = _group_with_a_reaped_leader(tmp_path)
    try:
        _REMEMBERED[leader.pid] = {tracked.pid: tracked}
        _stop(leader, "test leader", grace_s=5.0, groups=True)
        for member in (tracked, untracked):
            member.wait(10)
            assert not member.is_running()
    finally:
        for member in (tracked, untracked):
            with contextlib.suppress(psutil.NoSuchProcess):
                member.kill()


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


#: A stand-in window that is a real process: it answers /json/version with its
#: pid on the port in argv[1] and exits 0 once the file in argv[2] exists.
_WINDOW_SRC = """
import http.server, json, os, pathlib, sys, threading, time
port, close = int(sys.argv[1]), pathlib.Path(sys.argv[2])
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"pid": os.getpid()}).encode()
        self.send_response(200)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *_):
        pass
server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
server.allow_reuse_address = True
threading.Thread(target=server.serve_forever, daemon=True).start()
while not close.exists():
    time.sleep(0.05)
"""


def _until(condition: object, what: str, timeout_s: float = 30.0) -> None:
    import time

    deadline = time.monotonic() + timeout_s
    while not condition():  # type: ignore[operator]
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.05)


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
    import threading
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


class _StatusHandler(http.server.BaseHTTPRequestHandler):
    """Answers `/<code>` with that status; `/moved` redirects to `/200`."""

    def do_GET(self) -> None:
        if self.path == "/moved":
            self.send_response(302)
            self.send_header("Location", "/200")
        else:
            self.send_response(int(self.path.lstrip("/")))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture
def status_origin() -> Iterator[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _StatusHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


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
