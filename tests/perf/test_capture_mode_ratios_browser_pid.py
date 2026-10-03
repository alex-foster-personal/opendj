"""PERFMODE-15 capture_mode_ratios: real-browser-pid integration tests.

Split out of test_capture_mode_ratios.py (the 600-line test-file ratchet)
when PR #4553 strengthened these two tests' PID-attribution proof. Covers
`_capture_gig_then_trackify`/`_capture_trackify_leak` end to end against a
REAL spawned child process, with only the wall clock faked and the REAL
Darwin native footprint reader (Darwin-only; skipped elsewhere, matching
every other reference-Mac-gated capture in this repo). The pure
`_ProcessTreeSampler` unit tests and the session-protocol tests stay in
test_capture_mode_ratios.py.
"""

from __future__ import annotations

import itertools
import subprocess
import sys
from contextlib import suppress
from typing import Any
from unittest.mock import patch

import psutil
import pytest

from scripts.diagnostics.probe_native_metrics import DarwinProcessMetrics
from scripts.perf import capture_mode_ratios as cmr

_GIG_TRACKIFY_PROTOCOL_CHILD = """
import sys
print(sys.argv[1], flush=True)
print("GIG_READY", flush=True)
sys.stdin.readline()
print("TRACKIFY_READY", flush=True)
sys.stdin.readline()
print("DONE", flush=True)
sys.stdin.read()  # like node: stay alive until stdin reaches EOF
"""


def _spawn_gig_trackify_child(stable_ids_line: str) -> subprocess.Popen[str]:
    """A REAL child speaking the exact `mode_ratio_browser.mjs --mode
    gig-trackify` stdout/stdin protocol: GIG_STABLE_IDS then GIG_READY with no
    signal between them, one stdin line per subsequent handoff, DONE, then
    stdin EOF (Codex P1, PR #4034, discussion_r4138614642: `_fake_browser_proc`
    is a `MagicMock` with scripted `readline` return values, so it cannot
    catch a real stdout framing, ordering, or EOF regression the way this
    genuine subprocess pipe can)."""
    return subprocess.Popen(
        [sys.executable, "-c", _GIG_TRACKIFY_PROTOCOL_CHILD, stable_ids_line],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


_PYTHON = getattr(sys, "_base_executable", None) or sys.executable
_SLEEPER = [_PYTHON, "-c", "import time; time.sleep(30)"]

_DESCENDANT_PREFIX = (
    "import subprocess as _sp\n"
    f"_sp.Popen({_SLEEPER!r}, stdin=_sp.DEVNULL, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)\n"
)


def _spawn_with_descendant(script: str, *argv: str) -> subprocess.Popen[str]:
    """Like the plain protocol spawns above, but with a real OS child of
    its own (`sleep 30`), standing in for a Chromium descendant so
    `_ProcessTreeSampler.sample()` -- which excludes the root pid itself,
    since that's the Node launcher, not Chromium -- has something live to
    read. Killed explicitly by `_kill_tree` below; SIGKILL on the root
    alone does not cascade to it."""
    return subprocess.Popen(
        [sys.executable, "-c", _DESCENDANT_PREFIX + script, *argv],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


def _kill_tree(pid: int) -> None:
    try:
        root = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    for descendant in root.children(recursive=True):
        with suppress(psutil.NoSuchProcess):
            descendant.kill()
    with suppress(psutil.NoSuchProcess):
        root.kill()


# Same fake-monotonic semantics as test_capture_mode_ratios_engine_family: each
# `monotonic()` advances by `_PROBE_INTERVAL_S`, so 60s yields 12 probes at 5 s.
_FAKE_CLOCK_STEADY_DURATION_S = 120


def _fake_monotonic_ticking(step: float) -> Any:
    """A `time.monotonic` stand-in that advances by `step` every call.

    Lets `_sample_steady`/`_sample_leak` run their REAL deadline loop (real
    `_ProcessTreeSampler.sample()` calls against a REAL spawned child) to
    completion without waiting real wall-clock seconds to minutes -- only
    `time.monotonic`/`time.sleep` are faked, per Sol P1/BLOCKING, PR #4034,
    discussion_r4138712250: patching the SAMPLING functions themselves (the
    prior version of these two tests) bypasses the real launch-to-pid
    handoff AND the real sampler, so a broken integration between them
    could stay green. The native footprint reader is real (see
    `_RecordingNative` below), so only the clock is faked here.
    """
    counter = itertools.count()
    return lambda: next(counter) * step


class _RecordingNative:
    """Wraps the REAL `DarwinProcessMetrics`, recording every pid it reads.

    Sol P1/BLOCKING, PR #4553 (second round, no postable line anchor): a
    per-pid-aware FAKE still shares the defect verification.md warns
    against -- it is a mock of the exact measurement subject ("real
    positive per-mode values from that pid") these two tests claim to
    prove, and the old fake's `else: child_only_mb` branch answered for
    ANY non-launcher pid, so it could not by itself prove which pid was
    actually read. This wraps the real reader instead of replacing it, so
    every `.read()` call is a genuine `proc_pid_rusage` syscall against a
    real live process, and records the pid argument so the test can assert
    WHICH pid was read, not just that some positive number came back.
    """

    def __init__(self, real: DarwinProcessMetrics) -> None:
        self._real = real
        self.pids_read: list[int] = []

    def read(self, pid: int) -> Any:
        self.pids_read.append(pid)
        return self._real.read(pid)


_requires_darwin = pytest.mark.skipif(
    sys.platform != "darwin",
    reason=(
        "proves PID attribution against the REAL DarwinProcessMetrics reader "
        "(proc_pid_rusage), which only exists on macOS -- see "
        "_require_reference_mac's UNAVAILABLE convention elsewhere in this suite"
    ),
)


@_requires_darwin
@pytest.mark.requirement("PERFMODE-15")
def test_capture_gig_then_trackify_samples_the_browser_pid_not_the_frontend_url() -> None:
    """[if] a Gig/Trackify capture runs [then] it really samples the browser pid, not the URL, [else stop].

    This is the call-shape regression test for the claude-review finding:
    the prior implementation threaded the frontend URL into an HTTP probe
    of the packaged app's telemetry endpoint instead of the pid of the
    process `mode_ratio_browser.mjs` actually spawned. `_start_browser_session`
    returns a REAL spawned child speaking the genuine protocol
    (`_spawn_gig_trackify_child`); `_sample_steady` runs for REAL (not
    mocked) against it, over the REAL `DarwinProcessMetrics` reader (only
    the wall clock is faked), so this proves the real sampler produces
    real, positive per-mode values from that pid.

    Sol P1/BLOCKING, PR #4553 (two rounds): a per-pid-aware FAKE still
    shares the defect under test -- see `_RecordingNative`'s docstring.
    This version reads real `proc_pid_rusage` data through a recording
    spy and asserts the pids actually read are exactly the live
    descendant's, never the Node launcher's (the exact mixup
    `test_sample_excludes_the_root_launcher_pid_from_the_browser_only_kpi`
    guards at the unit level).
    """
    child = _spawn_with_descendant(
        _GIG_TRACKIFY_PROTOCOL_CHILD, 'GIG_STABLE_IDS ["a", "b", "c", "d"]'
    )
    engine = subprocess.Popen(_SLEEPER, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        native = _RecordingNative(DarwinProcessMetrics())

        with (
            patch("scripts.perf.capture_mode_ratios._start_browser_session", return_value=child),
            patch("scripts.perf.mode_ratio_sampler.DarwinProcessMetrics", return_value=native),
            patch("scripts.perf.capture_mode_ratios.time.sleep"),
            patch(
                "scripts.perf.capture_mode_ratios.time.monotonic",
                new=_fake_monotonic_ticking(cmr._PROBE_INTERVAL_S),
            ),
        ):
            gig, trackify, gig_stable_ids = cmr._capture_gig_then_trackify(
                "http://127.0.0.1:5273", _FAKE_CLOCK_STEADY_DURATION_S, engine.pid
            )

        assert gig_stable_ids == ["a", "b", "c", "d"]
        for result in (gig, trackify):
            assert result["sample_count"] >= 1.0
            assert result["browser_footprint_mb"] > 0.0
            assert result["engine_footprint_mb"] > 0.0
            assert result["footprint_mb"] == pytest.approx(
                result["browser_footprint_mb"] + result["engine_footprint_mb"]
            )
        # Real reads happened, and none of them were the launcher's own pid
        # (excluded by _ProcessTreeSampler.sample() by design): exactly the
        # one live browser descendant and the engine root were read.
        assert child.pid not in native.pids_read
        assert engine.pid in native.pids_read
        assert len(set(native.pids_read) - {engine.pid}) == 1
    finally:
        _kill_tree(child.pid)
        child.wait(timeout=5)
        engine.kill()
        engine.wait(timeout=5)
