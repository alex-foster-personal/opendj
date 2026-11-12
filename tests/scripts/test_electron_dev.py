"""The electron-dev launch plan: isolation from the installed app, per-worktree
ports and profiles, and headless display selection (scripts/electron_dev.py)."""

from __future__ import annotations

import contextlib
import os
import socket
import sys
from pathlib import Path

import pytest

from scripts.electron_dev import (
    CDP_PORT_OFFSET,
    ELECTRON_DIR,
    REPO_ROOT,
    _port_free,
    backend_reuse_refusal,
    needs_virtual_display,
)
from tests.scripts.electron_dev_helpers import (
    _RUN_BODY,
    _group_with_a_reaped_leader,
    _plan,
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
