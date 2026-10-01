"""Leak-capture integration tests for scripts.perf.capture_mode_ratios.

Split out of test_capture_mode_ratios.py (600-line test-file ratchet, PR
#4540). Shares the real-subprocess helpers that PR #4553 moved into
test_capture_mode_ratios_browser_pid.py.

ADR-NEW-trackify-leak-kpi-quiescent-baselines: the gating slope is fitted to
quiescent baselines. Each test runs the REAL `_capture_trackify_leak` against a
REAL child that speaks `mode_ratio_browser.mjs`'s leak protocol
(TRACKIFY_READY, CHECKPOINT -> QUIESCENT, RESUME -> RESUMED, NEXT -> DONE) and
a REAL descendant whose phys_footprint the real sampler reads. Only the clock
is faked: `time.sleep` advances it and really sleeps 10 ms.
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from scripts.diagnostics.probe_native_metrics import DarwinProcessMetrics
from scripts.perf import capture_mode_ratios as cmr
from scripts.perf.trackify_leak_series import LeakSeries
from tests.perf.test_capture_mode_ratios_browser_pid import _kill_tree, _RecordingNative, _requires_darwin

_REAL_SLEEP = time.sleep

# The descendant the sampler measures. While "playing" it holds a working set
# that grows track by track (4 MB more per track, like round 4's playlist of
# ever longer tracks). "Q" (quiesce) frees the working set; in `leak` mode it
# also keeps 4 MB for good, which is retention. Pages are mmap'd and touched so
# phys_footprint really moves, and unmapped so it really falls.
_WORKER = r"""
import mmap, sys
mode = sys.argv[1]
MB = 1 << 20
def touched(mb):
    block = mmap.mmap(-1, mb * MB)
    for offset in range(0, mb * MB, 4096):
        block[offset] = 1
    return block
retained = []
tracks = 1
working = touched(4)
for line in sys.stdin:
    command = line.strip()
    if command == "Q":
        working.close()
        working = None
        if mode == "leak":
            retained.append(touched(4))
    elif command == "R":
        tracks += 1
        working = touched(4 * tracks)
    print("ok", flush=True)
"""

# The protocol child (the sampler's root, which it excludes) relays each
# checkpoint to the descendant and answers only after the descendant has.
_LEAK_PROTOCOL_CHILD = r"""
import subprocess, sys
worker = subprocess.Popen([sys.executable, "-c", sys.argv[2], sys.argv[1]],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
def tell(command):
    worker.stdin.write(command + "\n")
    worker.stdin.flush()
    worker.stdout.readline()
tell("PING")
print("TRACKIFY_READY", flush=True)
for line in sys.stdin:
    command = line.strip()
    if command == "CHECKPOINT":
        tell("Q")
        print("QUIESCENT", flush=True)
    elif command == "RESUME":
        tell("R")
        print("RESUMED", flush=True)
    elif command == "NEXT":
        print("DONE", flush=True)
        break
    else:
        sys.exit(f"unexpected protocol line {command!r}")
sys.stdin.read()  # like node: stay alive until stdin reaches EOF
"""


class _SteppedClock:
    """`time.monotonic` that only moves when the capture sleeps."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        _REAL_SLEEP(0.01)


@contextmanager
def _leak_child(mode: str) -> Iterator[subprocess.Popen[str]]:
    child = subprocess.Popen(
        [sys.executable, "-c", _LEAK_PROTOCOL_CHILD, mode, _WORKER],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        yield child
    finally:
        _kill_tree(child.pid)
        child.wait(timeout=5)


def _capture(mode: str, native: _RecordingNative | None = None) -> tuple[LeakSeries, int]:
    """The series, and the pid of the protocol child (the sampler's excluded root)."""
    clock = _SteppedClock()
    reader = native if native is not None else DarwinProcessMetrics()
    with (
        _leak_child(mode) as child,
        patch("scripts.perf.capture_mode_ratios._start_browser_session", return_value=child),
        patch("scripts.perf.capture_mode_ratios.DarwinProcessMetrics", return_value=reader),
        patch("scripts.perf.capture_mode_ratios.time.sleep", new=clock.sleep),
        patch("scripts.perf.capture_mode_ratios.time.monotonic", new=clock.monotonic),
    ):
        return cmr._capture_trackify_leak("http://127.0.0.1:5273", cmr._MIN_LEAK_DURATION_S), child.pid


@_requires_darwin
@pytest.mark.requirement("PERFMODE-15")
def test_retention_that_survives_every_checkpoint_reads_over_budget() -> None:
    """[if] the tree keeps 4 MB more after every quiescent checkpoint [then] the retained slope is over 5 MB per 10 min, [else stop].

    Positive control: 4 MB per 300 s of played time is 8 MB per 10 min of real
    retention, so only a capture that samples this tree's real footprint at
    real checkpoints reports it.
    """
    series, _ = _capture("leak")
    assert len(series.baselines) == 13
    retained = series.retained_slope_mb_per_10min()
    assert retained > 5.0, f"expected the leak to read over budget, got {retained:.3f} ({series.summary()})"


@_requires_darwin
@pytest.mark.requirement("PERFMODE-15")
def test_a_working_set_that_grows_with_track_length_is_not_read_as_a_leak() -> None:
    """[if] only the playing track's working set grows [then] the retained slope stays flat while the raw slope reads over budget, [else stop].

    Negative control: this is round 4's shape on the fixed build, a raw slope
    driven by ever longer tracks. The raw assertion proves the run really had a
    growing working set for the retained slope to see past.
    """
    series, _ = _capture("working-set")
    retained = series.retained_slope_mb_per_10min()
    raw = series.raw_slope_mb_per_10min()
    assert abs(retained) < 2.0, f"expected a flat retained slope, got {retained:.3f} ({series.summary()})"
    assert raw > 5.0, f"control: the raw slope should read the working set as growth, got {raw:.3f}"


@_requires_darwin
@pytest.mark.requirement("PERFMODE-15")
def test_capture_trackify_leak_samples_the_browser_pid_not_the_frontend_url() -> None:
    """[if] a leak capture runs [then] it reads the browser descendant's real footprint, never the launcher's, [else stop].

    Sol P1/BLOCKING, PR #4553: a slope alone cannot prove which pid produced
    it. `_RecordingNative` wraps the REAL reader and records every pid it was
    asked for, so the assertion is on the measurement subject itself: exactly
    one pid, the descendant, and never the protocol child that stands in for
    the Node launcher.
    """
    native = _RecordingNative(DarwinProcessMetrics())
    series, launcher_pid = _capture("working-set", native)
    assert native.pids_read
    assert launcher_pid not in native.pids_read
    assert len(set(native.pids_read)) == 1
    assert all(mb > 0.0 for _, mb in series.raw + series.baselines)
