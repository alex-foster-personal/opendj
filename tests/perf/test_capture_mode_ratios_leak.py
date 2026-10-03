"""Leak-capture tests against the REAL browser helper: no fake child, no fake clock.

Codex P1/BLOCKING r4170456315, PR #4888: the earlier version of this file
monkeypatched `_start_browser_session`, `DarwinProcessMetrics` and the clock,
so it stayed green while `mode_ratio_browser.mjs`'s leak protocol or its real
quiescence could be broken. Every test here now drives the production
`_start_browser_session` -> `mode_ratio_browser.mjs --mode trackify-leak` ->
real Playwright Chromium -> a live frontend, over the real stdin/stdout
protocol (TRACKIFY_READY, CHECKPOINT -> QUIESCENT, RESUME -> RESUMED, NEXT ->
DONE) in a short window: two checkpoints, not the 1 h capture.

The slope arithmetic those fakes used to exercise is pure and lives, honestly
named as unit tests, in test_trackify_leak_series.py (a series whose baselines
keep rising reads as a leak; a growing working set over flat baselines does
not).

Prerequisites, each reported UNAVAILABLE by name when absent, never a pass:
- `MDT_PERF_LIVE_FRONTEND`: origin of a running frontend whose Trackify feed
  plays (e.g. the e2e fixture engine plus `pnpm dev`);
- `node` on PATH and Playwright's Chromium installed for the frontend;
- macOS, for the footprint test only: `_ProcessTreeSampler` reads
  `proc_pid_rusage` through `DarwinProcessMetrics`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from typing import cast

import psutil
import pytest

from scripts.diagnostics.probe_native_metrics import DarwinProcessMetrics
from scripts.perf import capture_mode_ratios as cmr
from tests.perf.test_capture_mode_ratios_browser_pid import _kill_tree, _RecordingNative

_FRONTEND_ENV = "MDT_PERF_LIVE_FRONTEND"
_CHECKPOINTS = 2
_CHROMIUM_PROBE = (
    "const { createRequire } = require('node:module');"
    "const path = require('node:path');"
    "const fs = require('node:fs');"
    "const req = createRequire(path.join(process.cwd(), 'package.json'));"
    "const { chromium } = req('@playwright/test');"
    "const exe = chromium.executablePath();"
    "if (!fs.existsSync(exe)) { console.error('missing ' + exe); process.exit(2); }"
)


# ----- prerequisites ---------------------------------------------------------


def _unavailable_reason() -> str | None:
    """Why the real helper cannot run here, or None when it can."""
    if not os.environ.get(_FRONTEND_ENV):
        return f"UNAVAILABLE: {_FRONTEND_ENV} names no live frontend whose Trackify feed plays"
    node = shutil.which("node")
    if node is None:
        return "UNAVAILABLE: node is not on PATH, and mode_ratio_browser.mjs runs under node"
    probe = subprocess.run(
        [node, "-e", _CHROMIUM_PROBE],
        cwd=cmr._FRONTEND_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if probe.returncode != 0:
        return f"UNAVAILABLE: Playwright Chromium is not installed for the frontend: {probe.stderr.strip()[-400:]}"
    return None


@pytest.fixture
def live_frontend() -> str:
    reason = _unavailable_reason()
    if reason is not None:
        pytest.skip(reason)
    return os.environ[_FRONTEND_ENV]


@pytest.fixture
def leak_session(live_frontend: str) -> Iterator[subprocess.Popen[str]]:
    """The production helper in trackify-leak mode, past TRACKIFY_READY."""
    proc = cmr._start_browser_session(live_frontend, "trackify-leak")
    try:
        cmr._read_browser_line(proc, "TRACKIFY_READY")
        yield proc
    finally:
        if proc.poll() is None:
            _kill_tree(proc.pid)
            proc.wait(timeout=30)


def _chromium_pids(launcher_pid: int) -> set[int]:
    return {child.pid for child in psutil.Process(launcher_pid).children(recursive=True)}


# ----- the real protocol ------------------------------------------------------


@pytest.mark.timeout(300)  # a protocol line that never arrives fails here, never hangs the suite
@pytest.mark.requirement("PERFMODE-15")
def test_the_real_helper_quiesces_and_resumes_at_every_checkpoint(leak_session: subprocess.Popen[str]) -> None:
    """[if] the real helper is driven through two checkpoints [then] it answers QUIESCENT, RESUMED, DONE and exits 0, [else stop].

    QUIESCENT is only printed after `quiesceTrackify` saw the deck unloaded
    before AND after garbage collection, and RESUMED only once a track is
    loaded and playing again, so each answer is the helper's own proof of the
    state it names. A Chromium tree must be live under the launcher while the
    capture runs: the sampler measures it, never the launcher.
    """
    assert _chromium_pids(leak_session.pid), "no Chromium process under the mode_ratio_browser launcher"
    for _ in range(_CHECKPOINTS):
        cmr._send_browser_line(leak_session, "CHECKPOINT")
        cmr._read_browser_line(leak_session, "QUIESCENT")
        cmr._send_browser_line(leak_session, "RESUME")
        cmr._read_browser_line(leak_session, "RESUMED")
    cmr._signal_browser(leak_session)
    cmr._finish_browser_session(leak_session)
    assert leak_session.returncode == 0


@pytest.mark.timeout(300)  # a protocol line that never arrives fails here, never hangs the suite
@pytest.mark.requirement("PERFMODE-15")
def test_the_real_helper_refuses_an_unknown_protocol_line(leak_session: subprocess.Popen[str]) -> None:
    """[if] the capture sends a line the leak protocol does not define [then] the helper exits nonzero naming it, [else stop].

    Negative control for the test above: a helper that answered anything at
    all would pass it, so this shows the real protocol can say no.
    """
    cmr._send_browser_line(leak_session, "BOGUS")
    with pytest.raises(RuntimeError, match="leak protocol expected CHECKPOINT or NEXT"):
        cmr._read_browser_line(leak_session, "QUIESCENT")


# ----- real footprints at real checkpoints ------------------------------------


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason=(
        "UNAVAILABLE: _ProcessTreeSampler reads phys_footprint through DarwinProcessMetrics "
        "(proc_pid_rusage), which only macOS provides"
    ),
)
@pytest.mark.timeout(300)  # a protocol line that never arrives fails here, never hangs the suite
@pytest.mark.requirement("PERFMODE-15")
def test_quiescent_baselines_read_chromium_footprint_never_the_launcher(
    leak_session: subprocess.Popen[str],
) -> None:
    """[if] real checkpoints sample the real helper tree [then] each baseline is positive Chromium footprint, not the launcher, [else stop].

    `_RecordingNative` wraps the REAL reader (constructor-injected, nothing
    patched) and records each pid read, so the assertion is on the measurement
    subject itself: Chromium descendants of the launcher, never the launcher.
    """
    native = _RecordingNative(DarwinProcessMetrics())
    sampler = cmr._ProcessTreeSampler(leak_session.pid, native=cast(DarwinProcessMetrics, native))
    chromium = _chromium_pids(leak_session.pid)
    baselines = [cmr._quiescent_baseline_mb(leak_session, sampler) for _ in range(_CHECKPOINTS)]
    chromium |= _chromium_pids(leak_session.pid)
    cmr._signal_browser(leak_session)
    cmr._finish_browser_session(leak_session)
    assert all(mb > 0.0 for mb in baselines), baselines
    assert native.pids_read
    assert leak_session.pid not in native.pids_read
    assert set(native.pids_read) & chromium, f"no pid read was a Chromium descendant: {sorted(set(native.pids_read))}"
