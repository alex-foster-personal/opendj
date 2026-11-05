"""The nightly perf job's scratch engine is stopped as a whole tree, on every OS.

``stop_scratch_engine`` used to signal a POSIX process group with
``os.killpg`` and swallow the errors, which raised AttributeError on Windows
and could not say whether anything survived. A real process tree stands in for
the engine here: a root that ignores the polite signal and starts a child that
ignores it too.

[if] the scratch engine tree outlives stop_scratch_engine [then] fail, [else stop].
"""

from __future__ import annotations

import subprocess
import sys
import time

import psutil
import pytest

from apps.shared.process_tree import spawn_tree
from scripts.perf.perf_kpi_nightly import stop_scratch_engine

pytestmark = pytest.mark.requirement("PROCTREE-7105")

_IGNORE = "import signal; signal.signal(getattr(signal, 'SIGBREAK', signal.SIGTERM), signal.SIG_IGN)"


def test_stop_scratch_engine_leaves_no_engine_process_behind() -> None:
    """[if] the scratch engine and its child ignore the polite signal [then] stop_scratch_engine still ends both, [else stop]."""
    child_source = _IGNORE + "; import time; time.sleep(120)"
    root_source = "; ".join([
        _IGNORE,
        "import subprocess, sys, time",
        f"c = subprocess.Popen([sys.executable, '-c', {child_source!r}])",
        "print(c.pid, flush=True)",
        "time.sleep(120)",
    ])
    proc, _root = spawn_tree([sys.executable, "-c", root_source], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout is not None
        child_pid = int(proc.stdout.readline())
        time.sleep(0.3)
        stop_scratch_engine(proc)
        assert proc.returncode is not None, "the engine handle was not reaped"
        assert not psutil.pid_exists(child_pid), f"engine child {child_pid} outlived the stop"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        if proc.stdout is not None:
            proc.stdout.close()


def test_stop_scratch_engine_refuses_a_handle_it_cannot_prove() -> None:
    """[if] the engine handle was not started by the seam [then] the stop refuses rather than guess, [else stop]."""
    from apps.shared.process_tree import ProcessTreeError

    stranger = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        with pytest.raises(ProcessTreeError):
            stop_scratch_engine(stranger)
        assert stranger.poll() is None, "a refused stop must not signal the process"
    finally:
        stranger.kill()
        stranger.wait(timeout=10)
