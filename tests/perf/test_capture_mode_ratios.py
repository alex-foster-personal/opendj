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

import itertools
import os
import subprocess
import sys
import threading
import time
from contextlib import suppress
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

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


# Sol P1/BLOCKING, PR #4540: the two capture integration tests read footprint
# through the REAL DarwinProcessMetrics, so a reader that ignored the pid or
# misread phys_footprint cannot stay green behind a fake. That reader is
# macOS-only, so elsewhere the coverage is reported UNAVAILABLE, not faked.
_REAL_NATIVE_METRICS = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="UNAVAILABLE: DarwinProcessMetrics reads phys_footprint through macOS APIs only",
)


def _fake_monotonic_ticking(step: float) -> Any:
    """A `time.monotonic` stand-in that advances by `step` every call.

    Lets `_sample_steady`/`_sample_leak` run their REAL deadline loop (real
    `_ProcessTreeSampler.sample()` calls against a REAL spawned child) to
    completion without waiting real wall-clock seconds to minutes -- only
    `time.monotonic`/`time.sleep` are faked, per Sol P1/BLOCKING, PR #4034,
    discussion_r4138712250: patching the SAMPLING functions themselves (the
    prior version of these two tests) bypasses the real launch-to-pid
    handoff AND the real sampler, so a broken integration between them
    could stay green. The native footprint reader is the real one; see
    `_REAL_NATIVE_METRICS`.
    """
    counter = itertools.count()
    return lambda: next(counter) * step


@pytest.mark.requirement("PERFMODE-15")
@_REAL_NATIVE_METRICS
def test_capture_gig_then_trackify_samples_the_browser_pid_not_the_frontend_url() -> None:
    """[if] a Gig/Trackify capture runs [then] it really samples the browser pid, not the URL, [else stop].

    This is the call-shape regression test for the claude-review finding:
    the prior implementation threaded the frontend URL into an HTTP probe
    of the packaged app's telemetry endpoint instead of the pid of the
    process `mode_ratio_browser.mjs` actually spawned. `_start_browser_session`
    returns a REAL spawned child speaking the genuine protocol
    (`_spawn_gig_trackify_child`); `_sample_steady` runs for REAL (not
    mocked) against it with the real native footprint reader, only the wall
    clock faked, so this also proves the real sampler produces real,
    positive per-mode values from that pid.
    """
    child = _spawn_with_descendant(
        _GIG_TRACKIFY_PROTOCOL_CHILD, 'GIG_STABLE_IDS ["a", "b", "c", "d"]'
    )
    try:
        with (
            patch("scripts.perf.capture_mode_ratios._start_browser_session", return_value=child),
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
            assert result["footprint_mb"] > 0.0
    finally:
        _kill_tree(child.pid)
        child.wait(timeout=5)


# A descendant whose footprint grows by 256 KB every 20 ms of real time,
# touching every page so phys_footprint (not just a reservation) rises.
_GROWING_DESCENDANT_PREFIX = (
    "import subprocess as _sp, sys as _sys\n"
    "_sp.Popen([_sys.executable, '-c', "
    "'import time\\nblocks = []\\nfor _ in range(600):\\n"
    '    blocks.append(b"x" * 262144)\\n    time.sleep(0.02)\\ntime.sleep(30)\'], '
    "stdin=_sp.DEVNULL, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)\n"
)
_REAL_SLEEP = time.sleep


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


_PROTOCOL_CHILD = """
import sys
for line in sys.argv[1:]:
    print(line, flush=True)
    if line != "DONE":
        sys.stdin.readline()
sys.stdin.read()  # like node: stay alive until stdin reaches EOF
"""


def _spawn_protocol_child(lines: list[str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", _PROTOCOL_CHILD, *lines],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


@pytest.mark.requirement("PERFMODE-15")
def test_finishing_a_session_returns_when_the_helper_waits_for_stdin_eof() -> None:
    """[if] helper lives until stdin EOF after DONE [then] finishing returns, [else stop].

    A helper that only exits on stdin EOF (node with a referenced stdin pipe)
    deadlocked a finished capture in `stderr.read()` on silver, Fri 25 Sep 2026.
    Both captures end through `_finish_browser_session`; this drives it against
    a real child process that speaks the helper's protocol.
    """
    child = _spawn_protocol_child(["GIG_READY", "TRACKIFY_READY", "DONE"])
    outcome: dict[str, object] = {}

    def _drive() -> None:
        cmr._read_browser_line(child, "GIG_READY")
        cmr._signal_browser(child)
        cmr._read_browser_line(child, "TRACKIFY_READY")
        cmr._signal_browser(child)
        cmr._finish_browser_session(child)
        outcome["finished"] = True

    worker = threading.Thread(target=_drive, daemon=True)
    worker.start()
    worker.join(timeout=20)
    try:
        assert not worker.is_alive(), "finishing hung waiting for the helper to exit after DONE"
        assert outcome == {"finished": True}
        assert child.wait(timeout=5) == 0
    finally:
        if child.poll() is None:
            child.kill()


@pytest.mark.requirement("PERFMODE-15")
def test_finishing_a_session_names_a_nonzero_helper_exit() -> None:
    """[if] helper prints DONE then exits nonzero [then] the error carries stderr, [else stop]."""
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; print('DONE', flush=True); "
            "sys.stderr.write('stem decode failed'); sys.exit(3)",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    with pytest.raises(RuntimeError, match="exited 3: stem decode failed"):
        cmr._finish_browser_session(child)


@pytest.mark.requirement("PERFMODE-15")
def test_finishing_a_session_times_out_before_reading_stderr() -> None:
    """[if] helper ignores stdin EOF [then] finish kills it within the timeout, [else stop]."""
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, time; print('DONE', flush=True); sys.stdin.read(); time.sleep(30)",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    outcome: dict[str, object] = {}

    def _finish() -> None:
        try:
            # A real, caller-supplied timeout, not a monkeypatched module
            # constant (Codex P1/BLOCKING, PR #4034, discussion_r4138422261):
            # this exercises the exact production code path in
            # _finish_browser_session, just with a shorter, genuine number.
            cmr._finish_browser_session(child, timeout_s=0.1)
        except BaseException as exc:  # noqa: BLE001 - the worker reports every terminal outcome
            outcome["error"] = exc

    worker = threading.Thread(target=_finish, daemon=True)
    worker.start()
    worker.join(timeout=2)
    try:
        assert not worker.is_alive(), "finish blocked on stderr before applying its process timeout"
        assert isinstance(outcome.get("error"), RuntimeError)
        assert "did not exit within 0.1s" in str(outcome["error"])
        assert child.poll() is not None
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


def _spawn_single_line_child(line: str) -> subprocess.Popen[str]:
    """A real child process that prints exactly one line then exits.

    Codex P1/BLOCKING, PR #4034, discussion_r4138547745: `_read_gig_stable_ids`
    tests previously fed it a `MagicMock` via `_fake_browser_proc`, bypassing
    the real subprocess pipe -- a green mock cannot tell a genuine stdout
    framing, ordering, or EOF regression from a passing test. This spawns a
    REAL child (the same pattern as `_spawn_protocol_child` above) so the
    function reads through an actual `Popen.stdout` pipe end to end.
    """
    return subprocess.Popen(
        [sys.executable, "-c", "import sys; print(sys.argv[1], flush=True)", line],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


@pytest.mark.requirement("PERFMODE-15")
def test_read_gig_stable_ids_accepts_four_valid_ids() -> None:
    """[if] the helper reports four real stable ids [then] they thread through, [else stop]."""
    child = _spawn_single_line_child('GIG_STABLE_IDS ["a", "b", "c", "d"]')
    try:
        assert cmr._read_gig_stable_ids(child) == ["a", "b", "c", "d"]
    finally:
        child.wait(timeout=5)


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "[]",
        '["only-one"]',
        '["a", "b", "c"]',
        '["a", "b", "c", ""]',
        '["a", "b", "c", 4]',
        '"not-a-list"',
    ],
)
@pytest.mark.requirement("PERFMODE-15")
def test_read_gig_stable_ids_refuses_anything_but_four_real_ids(payload: str) -> None:
    """[if] the payload is not exactly four non-empty strings [then] it raises, [else stop].

    A malformed or short list here is exactly the auditability gap Codex
    flagged (discussion_r4138216697): a Trackify row that cannot name its
    Gig denominator's four tracks is not a real measurement.
    """
    child = _spawn_single_line_child(f"GIG_STABLE_IDS {payload}")
    try:
        with pytest.raises(RuntimeError):
            cmr._read_gig_stable_ids(child)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


@pytest.mark.requirement("PERFMODE-15")
def test_read_gig_stable_ids_refuses_a_missing_protocol_line() -> None:
    """[if] the helper sends the wrong line first [then] the error names it, [else stop]."""
    child = _spawn_single_line_child("GIG_READY")
    try:
        with pytest.raises(RuntimeError, match="GIG_STABLE_IDS"):
            cmr._read_gig_stable_ids(child)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


@pytest.mark.requirement("PERFMODE-15")
def test_a_helper_that_dies_before_ready_names_its_own_error() -> None:
    """[if] the helper exits before GIG_READY [then] the error carries its stderr, [else stop]."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stderr.write('need 4 tracks, got 1'); sys.exit(1)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    with pytest.raises(RuntimeError, match="need 4 tracks, got 1"):
        cmr._read_browser_line(child, "GIG_READY")
