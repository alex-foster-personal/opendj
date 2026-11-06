"""Session-end child-process leak guard in tests/support/process_leaks.py.

- [if] a session leaves an unreaped sleep 30 child [then] it fails naming that cmdline, [else stop].
- [if] a session reaps the child it spawned [then] it stays green, [else stop].
- [if] a child alive before the snapshot is reported [then] broken, [else stop].
- [if] an xdist worker leaves an unreaped child [then] the controller session fails, [else stop].

-Cursor
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.support.process_leaks import _exempt_cmdline, leaked_processes, snapshot_children

pytestmark = pytest.mark.requirement("DEVOPS-8666")
pytest_plugins = ["pytester"]

REPO = Path(__file__).resolve().parents[2]
FAST_GRACE_S = 0.1
PIDFILE_ENV = "PROCESS_LEAK_GUARD_PIDFILE"
_SLEEP_ARGV = [sys.executable, "-c", "import time; time.sleep(30)"]


def _kill_pidfile(pidfile: Path) -> None:
    if not pidfile.is_file():
        return
    pid = int(pidfile.read_text(encoding="utf-8").strip())
    pidfile.unlink(missing_ok=True)
    try:
        import psutil

        try:
            p = psutil.Process(pid)
            p.kill()
            p.wait(timeout=5)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    except ImportError:
        sig = getattr(signal, "SIGKILL", signal.SIGTERM)
        with contextlib.suppress(ProcessLookupError, OSError, PermissionError):
            os.kill(pid, sig)


def _run_inner(pytester, monkeypatch, *, pidfile: Path | None = None, extra: list[str] | None = None) -> pytest.RunResult:
    if pidfile is not None:
        monkeypatch.setenv(PIDFILE_ENV, str(pidfile))
    monkeypatch.setenv("PYTHONPATH", str(REPO))
    args = [
        "-p",
        "tests.support.process_leaks",
        "-p",
        "no:cacheprovider",
        "-q",
    ]
    if extra:
        args = extra + args
    return pytester.runpytest_subprocess(*args)


def test_unreaped_sleep_fails_session_naming_cmdline(pytester, tmp_path, monkeypatch) -> None:
    """[if] sleep 30 is not reaped [then] the inner session fails naming it, [else stop]."""
    pytest.importorskip("psutil")
    pidfile = tmp_path / "leaked.pid"
    pytester.makepyfile(
        test_leak="""
        import os
        import subprocess
        import sys

        def test_leaves_sleep():
            proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            path = os.environ["PROCESS_LEAK_GUARD_PIDFILE"]
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(str(proc.pid))
        """
    )
    result = _run_inner(pytester, monkeypatch, pidfile=pidfile)
    try:
        assert result.ret != 0
        combined = result.stdout.str() + result.stderr.str()
        assert "sleep" in combined
    finally:
        _kill_pidfile(pidfile)


def test_reaped_child_session_stays_green(pytester, monkeypatch) -> None:
    """[if] the child is reaped [then] the inner session stays green, [else stop]."""
    pytest.importorskip("psutil")
    pytester.makepyfile(
        test_ok="""
        import subprocess
        import sys

        def test_reaps_child():
            proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            proc.kill()
            proc.wait()
        """
    )
    result = _run_inner(pytester, monkeypatch)
    result.assert_outcomes(passed=1)
    assert result.ret == 0


def test_xdist_worker_unreaped_child_fails_controller(pytester, tmp_path, monkeypatch) -> None:
    """[if] a worker leaks a child [then] the xdist controller run fails, [else stop]."""
    pytest.importorskip("psutil")
    pytest.importorskip("xdist")
    pidfile = tmp_path / "leaked_xdist.pid"
    pytester.makepyfile(
        test_leak="""
        import os
        import subprocess
        import sys

        def test_worker_leaves_sleep():
            proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            path = os.environ["PROCESS_LEAK_GUARD_PIDFILE"]
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(str(proc.pid))
        """
    )
    result = _run_inner(
        pytester,
        monkeypatch,
        pidfile=pidfile,
        extra=["-n", "1", "--max-worker-restart=0"],
    )
    try:
        assert result.ret != 0
        combined = result.stdout.str() + result.stderr.str()
        assert "sleep" in combined
        assert "process-leaks" in combined or "FAIL" in combined
    finally:
        _kill_pidfile(pidfile)


def test_xdist_worker_reaped_child_controller_stays_green(pytester, monkeypatch) -> None:
    """[if] the worker reaps its child [then] the xdist controller stays green, [else stop]."""
    pytest.importorskip("psutil")
    pytest.importorskip("xdist")
    pytester.makepyfile(
        test_ok="""
        import subprocess
        import sys

        def test_worker_reaps():
            proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            proc.kill()
            proc.wait()
        """
    )
    result = _run_inner(pytester, monkeypatch, extra=["-n", "1", "--max-worker-restart=0"])
    result.assert_outcomes(passed=1)
    assert result.ret == 0


def test_leaked_processes_names_unreaped_sleep() -> None:
    """[if] helper sees unreaped sleep [then] it names sleep, [else stop]."""
    pytest.importorskip("psutil")
    baseline = snapshot_children() or {}
    proc = subprocess.Popen(_SLEEP_ARGV)
    try:
        leaks = leaked_processes(baseline, grace_s=FAST_GRACE_S)
        assert any("sleep" in leak for leak in leaks), leaks
    finally:
        proc.kill()
        proc.wait()


def test_leaked_processes_empty_after_reap() -> None:
    """[if] the child is reaped [then] helper returns no leaks, [else stop]."""
    pytest.importorskip("psutil")
    baseline = snapshot_children() or {}
    proc = subprocess.Popen(_SLEEP_ARGV)
    proc.kill()
    proc.wait()
    assert leaked_processes(baseline, grace_s=FAST_GRACE_S) == []


def test_process_alive_before_baseline_is_not_a_leak() -> None:
    """[if] a pre-existing child is reported [then] broken, [else stop]."""
    pytest.importorskip("psutil")
    proc = subprocess.Popen(_SLEEP_ARGV)
    try:
        baseline = snapshot_children() or {}
        assert leaked_processes(baseline, grace_s=FAST_GRACE_S) == []
    finally:
        proc.kill()
        proc.wait()


def test_exempt_cmdline_matches_helpers_not_sleep_or_spawn_main() -> None:
    """[if] exempt strings or sleep/spawn_main are misclassified [then] broken, [else stop]."""
    assert _exempt_cmdline(("python", "-c", "from multiprocessing.resource_tracker import main"))
    assert _exempt_cmdline(("python", "-c", "from multiprocessing.forkserver import main"))
    assert _exempt_cmdline(("python", "-c", "import sys;exec(eval(sys.stdin.readline()))"))
    assert not _exempt_cmdline(("sleep", "30"))
    assert not _exempt_cmdline(("python", "-c", "from multiprocessing.spawn import spawn_main"))


def test_zombie_child_is_not_reported() -> None:
    """[if] an exited zombie is named a leak [then] broken, [else stop]."""
    pytest.importorskip("psutil")
    baseline = snapshot_children() or {}
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        for _ in range(100):
            if proc.poll() is not None:
                break
            time.sleep(0.01)
        assert proc.poll() is not None, "child should exit before leak check"
        assert leaked_processes(baseline, grace_s=FAST_GRACE_S) == []
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
