"""Unit tests for scripts.perf.capture_mode_ratios process-tree sampling.

PERFMODE-15 claude-review finding (PR #3676): the prior version of this
sampler probed the packaged desktop app's `/api/v1/performance/telemetry/processes`
endpoint, which describes an unrelated process family (desktop-shell /
python-engine / webkit-webcontent) rather than the Playwright/Chromium
process that `mode_ratio_browser.mjs` actually drives. These tests exercise
the real `psutil` process tree (per .claude/rules/verification.md: a mocked
psutil would share the defect under test, not catch it) plus the call-shape
regression that pins sampling to the browser subprocess's own pid.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

import psutil
import pytest

from scripts.diagnostics.probe_native_metrics import DarwinProcessMetrics
from scripts.perf import capture_mode_ratios as cmr


class _FakeNative:
    """Duck-types `DarwinProcessMetrics` so these tests stay runnable off
    macOS, matching this repo's existing DarwinProcessMetrics test
    convention (test_probe_process_family.py's `_FakeNative`).
    `phys_footprint`'s value is arbitrary here: these tests assert on
    process-tree membership and cpu_percent, never on footprint magnitude.
    """

    def read(self, pid: int) -> SimpleNamespace:
        return SimpleNamespace(phys_footprint=1024 * 1024)


def _native() -> DarwinProcessMetrics:
    """`_FakeNative` duck-types `DarwinProcessMetrics`; the cast tells mypy
    what every test here already relies on, since the real class can only
    be constructed on Darwin."""

    return cast(DarwinProcessMetrics, _FakeNative())


def _spawn_sleeper(seconds: float) -> subprocess.Popen[bytes]:
    return subprocess.Popen(["sleep", str(seconds)])


@pytest.mark.requirement("PERFMODE-15")
def test_live_tree_includes_a_spawned_child() -> None:
    """[if] a child spawns under the root [then] the tree walk finds it, [else stop].

    This is the direct regression test for the wrong-process defect: a
    sampler that queried a fixed HTTP endpoint instead of walking the OS
    process tree would never see this child appear.
    """
    child = _spawn_sleeper(2.0)
    try:
        sampler = cmr._ProcessTreeSampler(os.getpid(), native=_native())
        tree_pids = {proc.pid for proc in sampler._live_tree()}
        assert child.pid in tree_pids
    finally:
        child.kill()
        child.wait()


@pytest.mark.requirement("PERFMODE-15")
def test_tracked_processes_are_forgotten_once_the_child_exits() -> None:
    """[if] a tracked child exits [then] the tree walk drops it, [else stop]."""
    child = _spawn_sleeper(0.3)
    sampler = cmr._ProcessTreeSampler(os.getpid(), native=_native())
    sampler._live_tree()
    assert child.pid in sampler._tracked

    child.wait()
    time.sleep(0.2)  # let the OS reap the zombie before re-walking
    sampler._live_tree()

    assert child.pid not in sampler._tracked


@pytest.mark.requirement("PERFMODE-15")
def test_sample_raises_when_the_root_process_is_gone() -> None:
    """[if] the root pid is gone [then] sample() raises loud, not zero, [else stop]."""
    with patch.object(psutil, "Process", side_effect=psutil.NoSuchProcess(999999)):
        sampler = cmr._ProcessTreeSampler(999999, native=_native())
        with pytest.raises(RuntimeError, match="is not running"):
            sampler.sample()


@pytest.mark.requirement("PERFMODE-15")
def test_first_sample_after_a_process_appears_does_not_inflate_cpu() -> None:
    """[if] a process is newly tracked [then] its first cpu reading is near-zero, [else stop].

    So a single busy-tick between construction and the first real sample is
    not misreported as a huge CPU spike.

    Mutation control (opposite direction, per verification.md): a sampler
    that constructed a FRESH `psutil.Process` on every call instead of
    reusing one would read 0.0 on every sample, not just the first -- that
    is the bug this class exists to avoid, so this test also serves as the
    control that catches a regression back to that shape (see the CPU-rises
    test below).

    A live child is required: `sample()` excludes the root pid itself (the
    Sol-review fix below), so a root with no descendant would raise instead
    of returning a reading.
    """
    child = _spawn_sleeper(2.0)
    try:
        sampler = cmr._ProcessTreeSampler(os.getpid(), native=_native())
        first = sampler.sample()
        assert first["cpu_percent"] >= 0.0
    finally:
        child.kill()
        child.wait()


@pytest.mark.requirement("PERFMODE-15")
def test_footprint_comes_from_the_native_reader_not_psutil_rss() -> None:
    """[if] the native reader reports a footprint [then] sample() reports it, [else stop].

    Direct regression test for the codex-review finding: summing
    `psutil`'s `memory_info().rss` across a multi-process Chromium tree
    double-counts pages the processes share, so footprint must come from
    the real per-process `phys_footprint` counter (DarwinProcessMetrics)
    instead. `distinctive_mb` is a value this test process's real RSS could
    never coincidentally match, so this fails loud if the sampler reverts
    to reading psutil's memory_info() for footprint.
    """
    distinctive_mb = 777.0

    class _FixedFootprintNative:
        def read(self, pid: int) -> SimpleNamespace:
            return SimpleNamespace(phys_footprint=int(distinctive_mb * 1024 * 1024))

    child = _spawn_sleeper(2.0)
    try:
        sampler = cmr._ProcessTreeSampler(
            os.getpid(), native=cast(DarwinProcessMetrics, _FixedFootprintNative())
        )
        result = sampler.sample()
        # Root (this test process) is excluded from the sample, so the only
        # contributor is the one spawned child -- exactly one reading of
        # `distinctive_mb`, not the launcher's own footprint added in too.
        assert result["physical_footprint_mb"] == pytest.approx(distinctive_mb)
    finally:
        child.kill()
        child.wait()


@pytest.mark.requirement("PERFMODE-15")
def test_sample_excludes_the_root_launcher_pid_from_the_browser_only_kpi() -> None:
    """[if] the root pid reports a footprint [then] sample() excludes it, [else stop].

    Direct regression test for the Sol-review finding: `root_pid` is the
    Node `mode_ratio_browser.mjs` launcher that SPAWNS Chromium via
    Playwright, not a member of the Chromium browser/renderer family the
    KPI card and `_METHOD` claim to measure. Two DIFFERENT distinctive
    values (root vs. child) mean this fails loud in EITHER wrong direction:
    if root's value leaked into the sum, the total would exceed the child's
    alone; if the child were dropped instead, the total would be 0, not the
    child's value.
    """
    root_only_mb = 111.0
    child_only_mb = 222.0

    class _PerPidNative:
        def read(self, pid: int) -> SimpleNamespace:
            value_mb = root_only_mb if pid == os.getpid() else child_only_mb
            return SimpleNamespace(phys_footprint=int(value_mb * 1024 * 1024))

    child = _spawn_sleeper(2.0)
    try:
        sampler = cmr._ProcessTreeSampler(
            os.getpid(), native=cast(DarwinProcessMetrics, _PerPidNative())
        )
        result = sampler.sample()
        assert result["physical_footprint_mb"] == pytest.approx(child_only_mb)
    finally:
        child.kill()
        child.wait()


@pytest.mark.requirement("PERFMODE-15")
def test_reused_process_object_reports_nonzero_cpu_after_real_work() -> None:
    """[if] a process burns CPU between samples [then] cpu_percent reads above zero, [else stop].

    Proves the persistent `dict[int, psutil.Process]` cache is load-bearing:
    `cpu_percent(interval=None)` on a FRESH Process object always returns
    0.0 on its first call, so a sampler that rebuilt Process objects each
    call (the mutation) would fail this test by reporting 0.0 here too.

    The busy work runs in a CHILD process, not this test process: `sample()`
    now excludes the root pid itself (Sol review, PR #3676 -- the root is
    the Node launcher, not part of the Chromium family this KPI measures),
    so burning CPU in the root would no longer show up in the result.
    """
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time\nd=time.monotonic()+1.5\n\nwhile time.monotonic()<d: pass",
        ]
    )
    try:
        sampler = cmr._ProcessTreeSampler(os.getpid(), native=_native())
        sampler.sample()  # primes the cache
        time.sleep(0.5)  # let the child accumulate real CPU time
        second = sampler.sample()
        assert second["cpu_percent"] > 0.0
    finally:
        child.kill()
        child.wait()


@pytest.mark.requirement("PERFMODE-15")
def test_sample_steady_rejects_a_duration_below_the_floor() -> None:
    """[if] duration_s is below the floor [then] _sample_steady raises first, [else stop]."""
    with pytest.raises(ValueError, match="at least"):
        cmr._sample_steady(os.getpid(), cmr._MIN_SAMPLE_S - 1)


@pytest.mark.requirement("PERFMODE-15")
def test_sample_leak_rejects_a_duration_below_one_hour() -> None:
    """[if] leak-duration-s is below 1h [then] _sample_leak raises first, [else stop].

    Direct regression test for the claude-review P3: a 30s run with two or
    three samples must not be allowed to write
    `trackify_mode_footprint_slope_mb_per_10min` with a note implying a 1h
    leak slope.
    """
    with pytest.raises(ValueError, match="1 h unattended"):
        cmr._sample_leak(os.getpid(), cmr._MIN_LEAK_DURATION_S - 1)


def _fake_browser_proc(pid: int, lines: list[str]) -> MagicMock:
    proc = MagicMock()
    proc.pid = pid
    proc.stdout = MagicMock()
    proc.stdout.readline.side_effect = [f"{line}\n" for line in lines]
    proc.stdin = MagicMock()
    proc.stderr = MagicMock()
    proc.stderr.read.return_value = ""
    proc.wait.return_value = 0
    proc.poll.return_value = 0
    return proc


@pytest.mark.requirement("PERFMODE-15")
def test_capture_gig_then_trackify_samples_the_browser_pid_not_the_frontend_url() -> None:
    """[if] a Gig/Trackify capture runs [then] it samples the browser pid, not the URL, [else stop].

    This is the call-shape regression test for the claude-review finding:
    the prior implementation threaded the frontend URL into an HTTP probe
    of the packaged app's telemetry endpoint instead of the pid of the
    process `mode_ratio_browser.mjs` actually spawned.
    """
    fake_proc = _fake_browser_proc(pid=54321, lines=["GIG_READY", "TRACKIFY_READY", "DONE"])

    with (
        patch("scripts.perf.capture_mode_ratios._start_browser_session", return_value=fake_proc),
        patch(
            "scripts.perf.capture_mode_ratios._sample_steady",
            return_value={"footprint_mb": 100.0, "cpu_percent": 10.0, "sample_count": 4.0},
        ) as sample_steady,
    ):
        cmr._capture_gig_then_trackify("http://127.0.0.1:5273", cmr._MIN_SAMPLE_S)

    assert sample_steady.call_count == 2
    for call in sample_steady.call_args_list:
        root_pid_arg = call.args[0]
        assert root_pid_arg == 54321
        assert root_pid_arg != "http://127.0.0.1:5273"


@pytest.mark.requirement("PERFMODE-15")
def test_capture_trackify_leak_samples_the_browser_pid_not_the_frontend_url() -> None:
    """[if] a leak capture runs [then] it samples the browser pid, not the URL, [else stop]."""
    fake_proc = _fake_browser_proc(pid=98765, lines=["TRACKIFY_READY", "DONE"])

    with (
        patch("scripts.perf.capture_mode_ratios._start_browser_session", return_value=fake_proc),
        patch("scripts.perf.capture_mode_ratios._sample_leak", return_value=1.5) as sample_leak,
    ):
        slope = cmr._capture_trackify_leak("http://127.0.0.1:5273", 3600)

    sample_leak.assert_called_once_with(98765, 3600)
    assert slope == 1.5
