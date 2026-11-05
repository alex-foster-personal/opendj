"""The quit watchdog's reap ends every descendant, on every OS.

``reap_descendants`` is the last thing the engine runs before ``os._exit``, so
it is exercised here in a real process of its own: that process starts one
child that exits when asked and one that ignores the polite signal, reaps,
and prints what it found. The test then checks both children are gone. The
engine-level quit tests deliver SIGTERM to a whole engine and so run on POSIX
only; this one needs no signal delivery to the parent and runs everywhere.

[if] a descendant outlives the quit watchdog reap [then] fail, [else stop].
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

pytestmark = pytest.mark.requirement("PROCTREE-7105")

REPO = Path(__file__).resolve().parents[2]

_REAPER = "; ".join(
    [
        "import json, subprocess, sys, time",
        "from apps.shared.shutdown_bound import reap_descendants",
        "ignore = 'import signal, time; signal.signal(getattr(signal, \"SIGBREAK\", signal.SIGTERM), signal.SIG_IGN); time.sleep(120)'",
        "polite = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])",
        "stubborn = subprocess.Popen([sys.executable, '-c', ignore])",
        "time.sleep(0.5)",
        "found = reap_descendants()",
        "print(json.dumps({'pids': [polite.pid, stubborn.pid], 'found': found}), flush=True)",
    ]
)


def test_reap_descendants_leaves_no_child_running() -> None:
    """[if] the quit reap runs with a polite and a stubborn child [then] neither outlives it, [else stop]."""
    started = time.monotonic()
    out = subprocess.run(
        [sys.executable, "-c", _REAPER],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    took = time.monotonic() - started
    report = json.loads(out.stdout.strip().splitlines()[-1])
    assert len(report["found"]) >= 2, f"the reap found too little: {report}"
    survivors = [pid for pid in report["pids"] if psutil.pid_exists(pid)]
    assert survivors == [], f"children outlived the quit reap: {survivors} ({report})"
    assert took < 30, f"the reaping process took {took:.1f}s"
