"""PERFMODE-15 capture_mode_ratios: real-browser-pid integration tests.

Split out of test_capture_mode_ratios.py (the 600-line test-file ratchet)
when PR #4553 strengthened these two tests' PID-attribution proof. Covers
`_capture_gig_then_trackify`/`_capture_trackify_leak` end to end against a
REAL spawned child process, with only the wall clock and the Darwin-only
native footprint reader faked. The pure `_ProcessTreeSampler` unit tests and
the session-protocol tests stay in test_capture_mode_ratios.py.
"""

from __future__ import annotations

import itertools
import subprocess
import sys
from contextlib import suppress
from types import SimpleNamespace
from typing import Any, cast
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


_PROTOCOL_CHILD = """
import sys
for line in sys.argv[1:]:
    print(line, flush=True)
    if line != "DONE":
        sys.stdin.readline()
sys.stdin.read()  # like node: stay alive until stdin reaches EOF
"""


_DESCENDANT_PREFIX = (
    "import subprocess as _sp\n"
    "_sp.Popen(['sleep', '30'], stdin=_sp.DEVNULL, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)\n"
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


def _fake_monotonic_ticking(step: float) -> Any:
    """A `time.monotonic` stand-in that advances by `step` every call.

    Lets `_sample_steady`/`_sample_leak` run their REAL deadline loop (real
    `_ProcessTreeSampler.sample()` calls against a REAL spawned child) to
    completion without waiting real wall-clock seconds to minutes -- only
    `time.monotonic`/`time.sleep` are faked, per Sol P1/BLOCKING, PR #4034,
    discussion_r4138712250: patching the SAMPLING functions themselves (the
    prior version of these two tests) bypasses the real launch-to-pid
    handoff AND the real sampler, so a broken integration between them
    could stay green. `DarwinProcessMetrics` is swapped for a fake for the
    same reason every other test in this suite does (off-Darwin CI).
    """
    counter = itertools.count()
    return lambda: next(counter) * step


@pytest.mark.requirement("PERFMODE-15")
def test_capture_gig_then_trackify_samples_the_browser_pid_not_the_frontend_url() -> None:
    """[if] a Gig/Trackify capture runs [then] it really samples the browser pid, not the URL, [else stop].

    This is the call-shape regression test for the claude-review finding:
    the prior implementation threaded the frontend URL into an HTTP probe
    of the packaged app's telemetry endpoint instead of the pid of the
    process `mode_ratio_browser.mjs` actually spawned. `_start_browser_session`
    returns a REAL spawned child speaking the genuine protocol
    (`_spawn_gig_trackify_child`); `_sample_steady` runs for REAL (not
    mocked) against it, with only the wall clock and the Darwin-only native
    reader faked, so this also proves the real sampler produces real,
    positive per-mode values from that pid.

    Sol P1/BLOCKING, PR #4553, discussion at test_capture_mode_ratios.py:348:
    the old `_FakeNative`-style fake returned the SAME footprint for every
    pid, so the prior `footprint_mb > 0.0` assertion passed no matter which
    live pid was actually sampled -- the exact root-vs-descendant mixup
    `test_sample_excludes_the_root_launcher_pid_from_the_browser_only_kpi`
    guards at the unit level would still read as a pass here. A per-pid
    fake with two DISTINCT values, and pinning the assertion to the
    descendant's own value, closes that gap the same way that unit test
    does.
    """
    child = _spawn_with_descendant(
        _GIG_TRACKIFY_PROTOCOL_CHILD, 'GIG_STABLE_IDS ["a", "b", "c", "d"]'
    )
    try:
        root_only_mb = 333.0
        child_only_mb = 444.0

        class _PerPidNative:
            def read(self, pid: int) -> SimpleNamespace:
                value_mb = root_only_mb if pid == child.pid else child_only_mb
                return SimpleNamespace(phys_footprint=int(value_mb * 1024 * 1024))

        with (
            patch("scripts.perf.capture_mode_ratios._start_browser_session", return_value=child),
            patch(
                "scripts.perf.capture_mode_ratios.DarwinProcessMetrics",
                return_value=cast(DarwinProcessMetrics, _PerPidNative()),
            ),
            patch("scripts.perf.capture_mode_ratios.time.sleep"),
            patch(
                "scripts.perf.capture_mode_ratios.time.monotonic",
                new=_fake_monotonic_ticking(cmr._PROBE_INTERVAL_S),
            ),
        ):
            gig, trackify, gig_stable_ids = cmr._capture_gig_then_trackify(
                "http://127.0.0.1:5273", cmr._MIN_SAMPLE_S
            )

        assert gig_stable_ids == ["a", "b", "c", "d"]
        for result in (gig, trackify):
            assert result["sample_count"] >= 1.0
            # Pinned to the descendant's distinctive value, not merely > 0:
            # a regression that fed the root launcher's own pid into the KPI
            # would read root_only_mb here and fail just as loudly as a zero
            # footprint would.
            assert result["footprint_mb"] == pytest.approx(child_only_mb)
    finally:
        _kill_tree(child.pid)
        child.wait(timeout=5)


@pytest.mark.requirement("PERFMODE-15")
def test_capture_trackify_leak_samples_the_browser_pid_not_the_frontend_url(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """[if] a leak capture runs [then] it really samples the browser pid, not the URL, [else stop].

    Same real-sampler substitution as the gig/trackify test above (Sol
    P1/BLOCKING, PR #4034, discussion_r4138712250): `_start_browser_session`
    returns a REAL spawned child, and `_sample_leak` runs for real against
    it over a faked (not mocked) wall clock.

    Sol P1/BLOCKING, PR #4553, discussion at test_capture_mode_ratios.py:348:
    same gap as the gig/trackify test above -- the old fake's constant
    footprint cannot distinguish a correctly-sampled descendant from a
    wrongly-sampled root launcher, so a PID-attribution regression here
    would pass silently (the returned `slope` alone can't tell them apart
    either: both pids' constants are equally flat, so both equally produce a
    slope of 0.0). A per-pid fake, plus an assertion on `_sample_leak`'s own
    `first_mb=` debug line -- the only place the raw sampled value is
    observable, since `_capture_trackify_leak` returns only the derived
    slope -- proves the reading came from the descendant and not the root.
    """
    child = _spawn_with_descendant(_PROTOCOL_CHILD, "TRACKIFY_READY", "DONE")
    try:
        root_only_mb = 555.0
        child_only_mb = 666.0

        class _PerPidNative:
            def read(self, pid: int) -> SimpleNamespace:
                value_mb = root_only_mb if pid == child.pid else child_only_mb
                return SimpleNamespace(phys_footprint=int(value_mb * 1024 * 1024))

        with (
            patch("scripts.perf.capture_mode_ratios._start_browser_session", return_value=child),
            patch(
                "scripts.perf.capture_mode_ratios.DarwinProcessMetrics",
                return_value=cast(DarwinProcessMetrics, _PerPidNative()),
            ),
            patch("scripts.perf.capture_mode_ratios.time.sleep"),
            patch(
                "scripts.perf.capture_mode_ratios.time.monotonic",
                new=_fake_monotonic_ticking(cmr._PROBE_INTERVAL_S),
            ),
        ):
            slope = cmr._capture_trackify_leak("http://127.0.0.1:5273", cmr._MIN_LEAK_DURATION_S)

        assert isinstance(slope, float)
        stderr = capsys.readouterr().err
        assert f"first_mb={child_only_mb:.1f}" in stderr
        assert f"first_mb={root_only_mb:.1f}" not in stderr
    finally:
        _kill_tree(child.pid)
        child.wait(timeout=5)
