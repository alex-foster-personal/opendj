"""scripts/ci_reap_port_holders.sh frees a fixed port of an orphaned CI server.

Linux only: the script reads ``ss`` and ``/proc``. On CI it runs inside a job,
so every process here has a Runner.Worker ancestor; the tests override the
live-ancestor marker to exercise both branches from one process tree. CI
provenance is a cwd under a ``_work`` tree, so holders are started with an
explicit cwd: ``work_cwd`` for provenance, ``no_work_cwd`` for its absence.
``no_work_cwd`` deliberately does NOT use pytest's own ``tmp_path``: ADR-0029
puts CI's --basetemp under ``$RUNNER_TEMP``, which on a self-hosted runner is
itself under that runner's ``_work`` tree, so ``tmp_path`` is no longer a
reliable stand-in for "outside every _work tree" there.

Regression lines:
  - if an orphan holder with CI provenance is still alive after the script
    then broken
  - if a holder under a live job, or one without CI provenance, is signalled
    then broken
  - if a holder without CI provenance does not fail the step then broken
  - if a holder that ignores SIGTERM survives then broken
  - if the trailing command does not run once the ports are clear, or runs
    when they are not, then broken
  - if no port is given then the script must fail rather than reap nothing
  - if a holder without CI provenance releases during the foreign-holder
    wait and the step still fails then broken (issue #1613)
  - if a port-ownership registry marker exists but is never surfaced in the
    no-CI-provenance message then broken (issue #1613)
  - if a cross-uid holder whose pid `ss` cannot report is failed instead of
    waited for, or fails the step after releasing during that wait, then
    broken (issue #1613)
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="reads ss and /proc")

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "ci_reap_port_holders.sh"
ORPHAN = "no-such-ancestor-marker"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(port: int, cwd: Path, *, ignore_term: bool = False) -> subprocess.Popen:
    code = "import http.server, signal, sys\n"
    if ignore_term:
        code += "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    code += (
        "http.server.HTTPServer(('127.0.0.1', int(sys.argv[1])),"
        " http.server.SimpleHTTPRequestHandler).serve_forever()\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code, str(port)],
        cwd=cwd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(50):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return proc
        time.sleep(0.1)
    proc.kill()
    raise AssertionError(f"server never bound {port}")


def _invisible_owner_ss(tmp_path: Path) -> Path:
    """A real ``ss`` with the per-socket owner list removed.

    Unprivileged ``ss -p`` prints a listener owned by ANOTHER uid without its
    pid, which is the shape of the collision in issue #1613. One uid cannot
    create that shape, so this wrapper reproduces the visibility restriction
    over a genuinely held port while leaving everything else (the port, the
    holder, the wait) real.
    """
    real_ss = shutil.which("ss")
    assert real_ss, "ss is required for this suite"
    wrapper = tmp_path / "ss-no-owner"
    wrapper.write_text(
        f"#!/usr/bin/env bash\nexec {real_ss} \"$@\" | sed 's/ users:(.*//'\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return wrapper


def _run(
    *args: str,
    live_re: str,
    release_wait_s: str = "30",
    foreign_wait_s: str = "1",
    registry_dir: Path | None = None,
    ss_bin: Path | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(SCRIPT), *args],
        env={
            **os.environ,
            "MDT_CI_LIVE_ANCESTOR_RE": live_re,
            "MDT_CI_REAP_RELEASE_WAIT_S": release_wait_s,
            "MDT_CI_REAP_FOREIGN_WAIT_S": foreign_wait_s,
            "MDT_CI_PORT_OWNER_REGISTRY_DIR": str(registry_dir) if registry_dir else "/nonexistent",
            **({"MDT_CI_REAP_SS": str(ss_bin)} if ss_bin else {}),
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.fixture
def work_cwd(tmp_path: Path) -> Path:
    cwd = tmp_path / "_work" / "repo"
    cwd.mkdir(parents=True)
    return cwd


@pytest.fixture
def no_work_cwd() -> Path:
    """A cwd guaranteed to sit outside every ``_work`` tree, independent of
    where --basetemp (and so pytest's own ``tmp_path``) lands. See the module
    docstring: on a self-hosted runner ``tmp_path`` no longer guarantees this
    since ADR-0029 moved --basetemp under $RUNNER_TEMP."""
    cwd = Path(tempfile.mkdtemp(prefix="mdt-no-work-cwd-"))
    try:
        yield cwd
    finally:
        shutil.rmtree(cwd, ignore_errors=True)


def test_an_orphaned_ci_holder_is_terminated_and_named(work_cwd: Path) -> None:
    """if an orphan holder with CI provenance is still alive after the script then broken"""
    port = _free_port()
    proc = _serve(port, work_cwd)
    try:
        result = _run(str(port), live_re=ORPHAN)
        assert result.returncode == 0, result.stderr
        assert f"port {port} is held" in result.stdout
        assert f"pid={proc.pid}" in result.stdout
        assert proc.wait(timeout=10) == -signal.SIGTERM
    finally:
        proc.kill()


def test_a_holder_that_ignores_sigterm_is_killed(work_cwd: Path) -> None:
    """if a holder that ignores SIGTERM survives then broken"""
    port = _free_port()
    proc = _serve(port, work_cwd, ignore_term=True)
    try:
        result = _run(str(port), live_re=ORPHAN)
        assert result.returncode == 0, result.stderr
        assert "ignored SIGTERM" in result.stdout
        assert proc.wait(timeout=10) == -signal.SIGKILL
    finally:
        proc.kill()


def test_a_holder_under_a_live_job_is_left_alone(work_cwd: Path) -> None:
    """if a live job's holder is signalled then broken"""
    port = _free_port()
    proc = _serve(port, work_cwd)
    try:
        # This test process stands in for the worker: it is in the holder's ancestry.
        result = _run(
            str(port), live_re=f"pytest|{os.path.basename(sys.executable)}", release_wait_s="2"
        )
        assert result.returncode == 0, result.stderr
        assert "waiting up to 2s" in result.stdout and "left alone" in result.stdout
        time.sleep(1)
        assert proc.poll() is None, "live holder was killed"
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_a_live_holder_mid_teardown_is_waited_for_not_reported(work_cwd: Path) -> None:
    """if the previous job's server releasing the port a few seconds late trips the reaper
    then broken"""
    port = _free_port()
    proc = _serve(port, work_cwd)
    try:
        # Release the port 2 s after the reaper starts looking, as an engine
        # teardown does; the reaper must wait, see it freed, and run the command.
        import threading

        threading.Timer(2.0, proc.kill).start()
        result = _run(
            str(port), "--", "echo", "suite-ran",
            live_re=f"pytest|{os.path.basename(sys.executable)}", release_wait_s="15",
        )
        assert result.returncode == 0, result.stderr
        assert "released after" in result.stdout, result.stdout
        assert result.stdout.rstrip().endswith("suite-ran")
        assert "[ERROR]" not in result.stderr
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_a_holder_without_ci_provenance_fails_and_is_not_signalled(no_work_cwd: Path) -> None:
    """if a holder outside every _work tree is signalled, or does not fail the step, then broken"""
    port = _free_port()
    proc = _serve(port, no_work_cwd)
    try:
        result = _run(str(port), live_re=ORPHAN)
        assert result.returncode == 1
        assert "no CI provenance" in result.stderr and f"pid {proc.pid}" in result.stderr
        time.sleep(1)
        assert proc.poll() is None, "a holder without provenance was killed"
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_the_trailing_command_runs_only_once_ports_are_clear(
    work_cwd: Path, no_work_cwd: Path
) -> None:
    """if the command runs against a held port, or not at all on a clear one, then broken"""
    port = _free_port()
    proc = _serve(port, work_cwd)
    try:
        result = _run(str(port), "--", "echo", "suite-ran", live_re=ORPHAN)
        assert result.returncode == 0, result.stderr
        assert result.stdout.rstrip().endswith("suite-ran")
        assert result.stdout.index("terminating") < result.stdout.index("suite-ran")
    finally:
        proc.kill()
    blocked = _serve(port, no_work_cwd)
    try:
        result = _run(str(port), "--", "echo", "suite-ran", live_re=ORPHAN)
        assert result.returncode == 1 and "suite-ran" not in result.stdout
    finally:
        blocked.kill()
        blocked.wait(timeout=10)


def test_a_free_port_is_quiet_and_runs_the_command() -> None:
    result = _run(str(_free_port()), "--", "echo", "suite-ran", live_re="Runner\\.Worker")
    assert result.returncode == 0 and result.stdout == "suite-ran\n"


def test_no_ports_is_a_usage_error() -> None:
    """if no port is given then the script must fail loud"""
    result = subprocess.run(
        [str(SCRIPT), "--", "echo", "x"], capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 2 and "usage" in result.stderr


def test_a_port_still_held_by_a_stranger_after_the_wait_fails_before_the_command(
    no_work_cwd: Path,
) -> None:
    """[if] the release wait expires on a foreign holder and the command still runs [then] fail,
    [else stop]."""
    port = _free_port()
    proc = _serve(port, no_work_cwd)  # no CI provenance, and no live-job ancestor
    try:
        result = _run(str(port), "--", "echo", "suite-ran", live_re=ORPHAN, release_wait_s="1")
        assert result.returncode == 1 and "suite-ran" not in result.stdout
        assert "no CI provenance" in result.stderr
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_a_holder_that_vanishes_before_its_identity_is_read_does_not_abort(
    work_cwd: Path,
) -> None:
    """[if] a pid gone before its /proc entry is read aborts the reaper [then] fail, [else stop]."""
    port = _free_port()
    proc = _serve(port, work_cwd)
    try:
        # Kill it the instant the reaper starts: the listing may still name the
        # pid while /proc has already lost it.
        import threading

        threading.Timer(0.05, proc.kill).start()
        result = _run(str(port), "--", "echo", "suite-ran", live_re=ORPHAN, release_wait_s="10")
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.rstrip().endswith("suite-ran"), result.stdout
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_a_foreign_holder_that_releases_during_the_wait_still_runs_the_command(
    no_work_cwd: Path,
) -> None:
    """if a holder without CI provenance releases during the foreign-holder
    wait and the step still fails then broken (issue #1613)"""
    port = _free_port()
    proc = _serve(port, no_work_cwd)  # no CI provenance, and no live-job ancestor
    try:
        import threading

        threading.Timer(1.0, proc.kill).start()
        result = _run(
            str(port), "--", "echo", "suite-ran", live_re=ORPHAN, foreign_wait_s="10"
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.rstrip().endswith("suite-ran"), result.stdout
        assert "[ERROR]" not in result.stderr, result.stderr
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_a_registry_ownership_marker_is_named_in_the_no_ci_provenance_message(
    tmp_path: Path, no_work_cwd: Path
) -> None:
    """if a port-ownership registry marker exists but is never surfaced in the
    no-CI-provenance message then broken (issue #1613)"""
    port = _free_port()
    proc = _serve(port, no_work_cwd)
    registry_dir = tmp_path / "registry"
    registry_dir.mkdir()
    marker = registry_dir / f"{port}-{os.getuid()}.owner"
    marker.write_text(
        "worktree=/home/dev/code/music-dj-tools-wt-demo-1613\n"
        "claimed_at=2026-09-10T00:00:00+00:00\n"
    )
    try:
        result = _run(
            str(port), live_re=ORPHAN, foreign_wait_s="1", registry_dir=registry_dir
        )
        assert result.returncode == 1
        assert "music-dj-tools-wt-demo-1613" in result.stdout, result.stdout
        assert "music-dj-tools-wt-demo-1613" in result.stderr, result.stderr
    finally:
        proc.kill()
        proc.wait(timeout=10)


@pytest.mark.requirement("INFRA-08")
def test_a_cross_uid_holder_is_waited_for_before_the_step_fails(tmp_path: Path) -> None:
    """[if] another uid holds the port [then] the step waits before failing, [else stop].

    CONTROL: if a holder whose pid `ss` cannot report fails the step the
    moment it is seen, instead of after the bounded foreign-holder wait, then
    the retry path of issue #1613 is unreachable for the collision it exists
    for."""
    port = _free_port()
    proc = _serve(port, tmp_path)  # real holder; only its pid is hidden
    try:
        result = _run(
            str(port),
            live_re=ORPHAN,
            foreign_wait_s="1",
            ss_bin=_invisible_owner_ss(tmp_path),
        )
        assert result.returncode == 1, result.stdout + result.stderr
        assert "cannot see" in result.stderr, result.stderr
        assert "waiting up to" in result.stdout, result.stdout
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_a_foreign_holder_is_waited_for_only_once(tmp_path: Path) -> None:
    """if a foreign holder that never releases is waited for in the first pass
    AND again in the final pass then broken -- the verdict is already a
    failure, so the second full wait only holds the host lock longer to reach
    the same answer (issue #1613)"""
    port = _free_port()
    proc = _serve(port, tmp_path)
    try:
        result = _run(
            str(port),
            live_re=ORPHAN,
            foreign_wait_s="1",
            ss_bin=_invisible_owner_ss(tmp_path),
        )
        assert result.returncode == 1, result.stdout + result.stderr
        assert result.stdout.count("waiting up to") == 1, result.stdout
        assert "after the foreign-holder wait" in result.stderr, result.stderr
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_the_cross_uid_path_runs_under_a_real_second_uid_when_one_is_available(
    tmp_path: Path,
) -> None:
    """if this box can run the reaper as a second uid, the port must be seen
    and waited for across the real uid boundary (issue #1613).

    SKIPPED, never passed, where that capability is absent: the collision this
    exercises needs two uids, and a same-uid substitute is not evidence about
    the boundary. unprivileged `ss` already omits another uid's pid, and
    unprivileged /proc and the marker directory's ownership are what the
    change is for.
    """
    if not shutil.which("setpriv"):
        pytest.skip("capability unavailable: setpriv is not installed")
    if os.geteuid() != 0:
        pytest.skip(
            "capability unavailable: reaching a second uid needs root (this test "
            f"runs as uid {os.geteuid()}), so the cross-uid boundary is not exercised here"
        )
    port = _free_port()
    proc = _serve(port, tmp_path)
    try:
        import threading

        threading.Timer(1.0, proc.kill).start()
        result = subprocess.run(
            [
                "setpriv",
                "--reuid=65534",
                "--regid=65534",
                "--clear-groups",
                str(SCRIPT),
                str(port),
                "--",
                "echo",
                "suite-ran",
            ],
            env={
                **os.environ,
                "MDT_CI_LIVE_ANCESTOR_RE": ORPHAN,
                "MDT_CI_REAP_RELEASE_WAIT_S": "30",
                "MDT_CI_REAP_FOREIGN_WAIT_S": "10",
                "MDT_CI_PORT_OWNER_REGISTRY_DIR": str(tmp_path / "owners"),
            },
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.rstrip().endswith("suite-ran"), result.stdout
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_a_cross_uid_holder_that_releases_during_the_wait_runs_the_command(
    tmp_path: Path,
) -> None:
    """if a cross-uid holder whose pid `ss` cannot report releases during the
    foreign-holder wait and the step still fails then broken (issue #1613)"""
    port = _free_port()
    proc = _serve(port, tmp_path)
    try:
        import threading

        threading.Timer(1.0, proc.kill).start()
        result = _run(
            str(port),
            "--",
            "echo",
            "suite-ran",
            live_re=ORPHAN,
            foreign_wait_s="10",
            ss_bin=_invisible_owner_ss(tmp_path),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.rstrip().endswith("suite-ran"), result.stdout
        assert "[ERROR]" not in result.stderr, result.stderr
    finally:
        proc.kill()
        proc.wait(timeout=10)
