"""Leak-capture integration test for scripts.perf.capture_mode_ratios.

Split out of test_capture_mode_ratios.py (600-line test-file ratchet, PR
#4540). Shares that module's real-subprocess protocol helpers.
"""

from __future__ import annotations

import subprocess
import sys
import time
from unittest.mock import patch

import pytest

from scripts.perf import capture_mode_ratios as cmr
from tests.perf.test_capture_mode_ratios import (
    _PROTOCOL_CHILD,
    _REAL_NATIVE_METRICS,
    _fake_monotonic_ticking,
    _kill_tree,
)

_REAL_SLEEP = time.sleep


# A descendant whose footprint grows by 256 KB every 20 ms of real time,
# touching every page so phys_footprint (not just a reservation) rises.
_GROWING_DESCENDANT_PREFIX = (
    "import subprocess as _sp, sys as _sys\n"
    "_sp.Popen([_sys.executable, '-c', "
    "'import time\\nblocks = []\\nfor _ in range(600):\\n"
    '    blocks.append(b"x" * 262144)\\n    time.sleep(0.02)\\ntime.sleep(30)\'], '
    "stdin=_sp.DEVNULL, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)\n"
)


@_REAL_NATIVE_METRICS
@pytest.mark.requirement("PERFMODE-15")
def test_capture_trackify_leak_measures_a_growing_browser_descendant() -> None:
    """[if] the sampled browser tree grows steadily [then] the leak capture reports a positive slope, [else stop].

    Sol P1/BLOCKING, PR #4540: asserting only that the slope is a float let a
    `_sample_leak` that returned a constant pass. Here a REAL descendant grows
    by about 12.5 MB/s of real time, each tick really sleeps 20 ms, and the
    wall clock the slope is computed over advances one probe interval per
    tick. So only a sampler that reads this tree's real footprint can report
    the positive slope asserted below.
    """
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _GROWING_DESCENDANT_PREFIX + _PROTOCOL_CHILD,
            "TRACKIFY_READY",
            "DONE",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        with (
            patch("scripts.perf.capture_mode_ratios._start_browser_session", return_value=child),
            patch("scripts.perf.capture_mode_ratios.time.sleep", new=lambda _s: _REAL_SLEEP(0.02)),
            patch(
                "scripts.perf.capture_mode_ratios.time.monotonic",
                new=_fake_monotonic_ticking(cmr._PROBE_INTERVAL_S),
            ),
        ):
            slope = cmr._capture_trackify_leak("http://127.0.0.1:5273", cmr._MIN_LEAK_DURATION_S)

        assert slope > 1.0, f"expected a clearly positive MB/10min slope, got {slope}"
    finally:
        _kill_tree(child.pid)
        child.wait(timeout=5)
