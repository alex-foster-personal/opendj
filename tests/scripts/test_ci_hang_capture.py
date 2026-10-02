"""The e2e gate's hang capture for the webkit deck-load smoke (issue #4281).

`scripts/ci_hang_capture.py` wraps the deck-load smoke's playwright run and, if it is
still running at set offsets, records each matching descendant's threads and a gdb
backtrace. Everything here runs real processes: a real child that spins or sleeps,
read through the real /proc. Nothing is mocked.

Regression lines:
  - if the wrapper's exit code differs from the command's then broken (a capture
    step could launder a red playwright run, or red a green one)
  - if a command that finishes before the first offset leaves a capture then broken
    (every green run would upload noise)
  - if a spinning child reads BLOCKED, or a sleeping one BUSY, then broken (#4281
    would be sent to the wrong layer)
  - if a pattern that matches nothing writes anything but UNKNOWN then broken (an
    empty capture would read as "the WebProcess was fine")
  - if a missing gdb is reported as anything but UNKNOWN then broken
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts import ci_hang_capture

pytestmark = pytest.mark.skipif(
    not Path("/proc").is_dir(), reason="hang capture reads Linux /proc; capture is Linux-only"
)

SPIN = "import time\nend = time.monotonic() + 6\nwhile time.monotonic() < end: pass\n"
SLEEP = "import time\ntime.sleep(6)\n"


def _child(script: str) -> list[str]:
    # The wrapped command is a launcher whose CHILD runs the script, the same shape
    # as pnpm -> playwright -> WebKitWebProcess: the matched process is a
    # grandchild of the wrapper, so the descendant walk is what finds it.
    launcher = f'import subprocess, sys\nsubprocess.run([sys.executable, "-c", {script!r}], check=True)\n'
    return [sys.executable, "-c", launcher]


def _comm_of_interpreter() -> str:
    run = subprocess.Popen([sys.executable, "-c", SLEEP])
    try:
        return ci_hang_capture.process_comm(run.pid) or ""
    finally:
        run.kill()
        run.wait()


def _run(tmp_path: Path, script: str, *, match: str, at: str = "1", gdb: str = "gdb") -> int:
    return ci_hang_capture.main(
        [
            "--out",
            str(tmp_path / "cap"),
            "--at",
            at,
            "--match",
            match,
            "--sample",
            "1",
            "--gdb",
            gdb,
            "--",
            *_child(script),
        ]
    )


def test_exit_code_passes_through_and_fast_command_leaves_no_capture(tmp_path: Path) -> None:
    code = ci_hang_capture.main(
        ["--out", str(tmp_path / "cap"), "--at", "5", "--", sys.executable, "-c", "raise SystemExit(3)"]
    )
    assert code == 3
    assert not (tmp_path / "cap").exists()


def test_green_command_stays_green(tmp_path: Path) -> None:
    # Control for the overshoot of the test above: a wrapper that always returned
    # nonzero would pass "exit 3 is 3" only if it also returned 3 here.
    code = ci_hang_capture.main(["--out", str(tmp_path / "cap"), "--at", "5", "--", sys.executable, "-c", "pass"])
    assert code == 0


def test_spinning_child_reads_busy(tmp_path: Path) -> None:
    comm = _comm_of_interpreter()
    assert comm, "control: the interpreter child must have a readable comm"
    assert _run(tmp_path, SPIN, match=comm, gdb="/nonexistent/gdb") == 0
    text = (tmp_path / "cap" / "capture-1s.txt").read_text()
    assert "BUSY" in text, text
    assert "UNKNOWN: no process" not in text
    # Root (launcher, blocked in wait) and grandchild (spinning) both match: the
    # grandchild must be the BUSY one, so at least two pids are reported.
    assert text.count("pid ") >= 2, text


def test_sleeping_child_reads_blocked(tmp_path: Path) -> None:
    comm = _comm_of_interpreter()
    assert _run(tmp_path, SLEEP, match=comm, gdb="/nonexistent/gdb") == 0
    text = (tmp_path / "cap" / "capture-1s.txt").read_text()
    assert "BLOCKED" in text, text
    assert " BUSY " not in text, text


def test_no_matching_descendant_is_unknown_naming_the_pattern(tmp_path: Path) -> None:
    assert _run(tmp_path, SLEEP, match="NoSuchProcessName") == 0
    text = (tmp_path / "cap" / "capture-1s.txt").read_text()
    assert "UNKNOWN: no process in the tree of pid" in text
    assert "'NoSuchProcessName'" in text
    assert "processes seen:" in text


def test_missing_gdb_is_unknown(tmp_path: Path) -> None:
    comm = _comm_of_interpreter()
    assert _run(tmp_path, SLEEP, match=comm, gdb="/nonexistent/gdb") == 0
    text = (tmp_path / "cap" / "capture-1s.txt").read_text()
    assert "gdb: UNKNOWN ('/nonexistent/gdb' is not installed" in text


def test_two_offsets_write_two_captures(tmp_path: Path) -> None:
    comm = _comm_of_interpreter()
    assert _run(tmp_path, SLEEP, match=comm, at="1,3", gdb="/nonexistent/gdb") == 0
    assert sorted(p.name for p in (tmp_path / "cap").iterdir()) == [
        "capture-1s.txt",
        "capture-3s.txt",
    ]


def test_deckload_smoke_step_runs_under_the_capture() -> None:
    """The e2e gate's deck-load smoke step wraps playwright in the capture, inside the
    host lock and port reaper, writing outside the step's own playwright outputDir."""
    workflow = (Path(__file__).resolve().parents[2] / ".github/workflows/e2e.yml").read_text()
    start = workflow.index("- name: Deck-load smoke (chromium + webkit, production build)")
    step = workflow[start : workflow.index("- name:", start + 10)]
    run = step[step.index("run: |") :]
    assert "scripts/ci_hang_capture.py" in run
    assert run.index("ci_host_lock.sh") < run.index("ci_hang_capture") < run.index("playwright test")
    assert "--out test-results/hang-capture" in run
    assert "--output test-results/deckload-smoke" in run
