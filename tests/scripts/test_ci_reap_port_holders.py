"""scripts/ci_reap_port_holders.sh frees a fixed port of an orphaned CI server.

Linux only: the script reads ``ss`` and ``/proc``. On CI it runs inside a job,
so every process here has a Runner.Worker ancestor; the tests override the
live-ancestor marker to exercise both branches from one process tree. CI
provenance is a cwd under a ``_work`` tree, so holders are started with an
explicit cwd.

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
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
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


def _run(*args: str, live_re: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(SCRIPT), *args],
        env={**os.environ, "MDT_CI_LIVE_ANCESTOR_RE": live_re},
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
        result = _run(str(port), live_re=f"pytest|{os.path.basename(sys.executable)}")
        assert result.returncode == 0, result.stderr
        assert "left alone" in result.stdout
        time.sleep(1)
        assert proc.poll() is None, "live holder was killed"
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_a_holder_without_ci_provenance_fails_and_is_not_signalled(tmp_path: Path) -> None:
    """if a holder outside every _work tree is signalled, or does not fail the step, then broken"""
    port = _free_port()
    proc = _serve(port, tmp_path)
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
    work_cwd: Path, tmp_path: Path
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
    blocked = _serve(port, tmp_path)
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
