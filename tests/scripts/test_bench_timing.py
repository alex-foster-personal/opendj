"""The bench timing instrument must not charge a process for time it did not run.

Regression cover for a false red that cost every local agent time whenever the
fleet was busy: ``scripts/bench/waveform_materialization.py`` gated on elapsed
wall time, so one commit scored 6.28, 14.29 and 33.53 percent improvement across
three runs while this box's load average climbed 42 -> 68 on Mon 31 Aug 2026.

Two of the four tests carry the claim, and they are not redundant. The sleep
case is the sharp one: it is
deterministic, it needs no scheduler cooperation, and it fails outright against
the wall-clock instrument. The background-load case is the one that reproduces
the reported shape, and because it depends on the OS actually creating
contention it carries its own control: if wall time did not move between the two
phases, the run reports UNKNOWN via skip rather than a green it did not earn.

Both were run against the pre-fix instrument before being trusted (``time_call``
temporarily reading ``perf_counter_ns`` into ``cpu_ms``); a test that has not
been shown to go red is not cover.
"""

from __future__ import annotations

import os
import statistics
import subprocess
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

import pytest

from scripts.bench.timing import interleaved_samples, time_call

#: A stall long enough that no clock resolution question arises.
STALL_S = 0.25

#: Rounds of integer work per timed call. Sized for roughly 40-80ms of CPU, which
#: is the same order as the real kernels (16.0ms Python / 7.0ms Rust at 38400
#: points) and far above process_time's resolution on fast CI runners.
BUSY_ROUNDS = 400_000

#: Samples per phase. Both instruments are reduced by the SAME statistic, the
#: median, on purpose: it is the statistic the bench itself gates on, and using
#: one reduction for CPU and another for wall would let the two swings differ for
#: a reason that has nothing to do with which clock was read.
SAMPLES_PER_PHASE = 7

#: Competing processes to spawn. Sized to oversubscribe a box that is already
#: busy: a run on an idle Mac needs far fewer, but the machine this defect
#: actually bites on carries a load average in the 40s to 60s, and 2x the core
#: count barely moves wall time there (measured 1.33x, which tripped the UNKNOWN
#: skip below rather than testing anything). 4x is what a 4-vCPU CI runner
#: needed to move its wall time 9.71x.
#:
#: The MULTIPLE is the experimental condition, so a cap that cuts into it does
#: not merely spawn fewer hogs, it changes what is being measured. The old cap
#: of 32 did that on anything above 8 cores, and CI's move to a 16-core
#: self-hosted runner made it bite: 32 hogs there is 2x, not 4x, and the verdict
#: became a coin flip on an IDLE box (3/5 pass, median 3.4x against the 3.0x
#: claimed below). Restoring 4x on that same box: 5/5 pass, median 7.4x. CPU
#: swing measured 2.46-2.52x in BOTH conditions, so the cap never moved the
#: quantity under test - it starved the wall-side contrast the claim is measured
#: AGAINST, and the UNKNOWN control below does not catch that because wall still
#: swung 5x, far above its floor. The cap that remains is a resource guard for a
#: machine much larger than any in this fleet, placed where it cannot silently
#: eat the multiple again at the sizes actually in use.
HOG_COUNT = min(4 * (os.cpu_count() or 4), 256)

#: The hog itself. A shell spin loop, split out so a test can spawn the same real
#: loop under a unique marker and count it with pgrep.
HOG_LOOP = "while :; do :; done"
HOG_COMMAND = ["/bin/sh", "-c", HOG_LOOP]

#: A path that cannot be exec'd, so a real subprocess.Popen raises a real
#: FileNotFoundError. No double: the failure is the OS refusing the exec.
UNSPAWNABLE = ["/nonexistent/mdt-there-is-no-such-binary"]

#: Real children the partial-spawn test starts before the unspawnable one.
HOGS_BEFORE_THE_FAILURE = 3

#: Wall time must move at least this much BETWEEN the two phases, in either
#: direction, or the machine's conditions demonstrably did not change and the
#: CPU-side assertion below would prove nothing. The direction is deliberately
#: not pinned: on a box the agent fleet is already hammering, the quiet phase is
#: routinely the contended one (measured here: wall 417.5 -> 30.5ms while CPU
#: held at 10.6 -> 10.2ms), and a floor that only accepts inflation would call
#: that a failed control when it is the cleanest demonstration of the defect.
WALL_SWING_FLOOR = 1.5

#: How much less load-sensitive CPU time must be than wall time, measured on each
#: instrument's EXCESS over an unchanged 1.0x.
#:
#: This is deliberately a RELATIVE invariant and not an absolute drift ceiling.
#: An absolute one was tried first at 1.35x and was WRONG, not merely tight: CPU
#: time is not load-invariant, because contention makes the same instruction
#: stream genuinely occupy the CPU longer via shared-cache thrashing and SMT
#: siblings. A 4-vCPU ubuntu-latest runner measured cpu 12.3 -> 22.9ms (1.87x)
#: against wall 12.3 -> 119.3ms (9.71x), so the ceiling reddened CI for a
#: property the fix never claimed. Excess ratios there: 8.71 / 0.87 = 10x. On a
#: 10-core Mac under fleet load: 0.86 / 0.04 = 21x. A wall-clock instrument reads
#: the same number on both sides and scores exactly 1x, so 3x cannot be reached
#: by the source this test exists to catch, on any machine, without needing a
#: per-machine number that would rot.
LOAD_SENSITIVITY_RATIO = 3.0


def _busy(rounds: int = BUSY_ROUNDS) -> int:
    """Deterministic integer work: the same instruction count on every call."""
    total = 0
    for index in range(rounds):
        total += index * index % 7
    return total


def _phase() -> tuple[float, float]:
    """(median cpu_ms, median wall_ms) over SAMPLES_PER_PHASE calls of _busy."""
    samples = interleaved_samples({"busy": _busy}, SAMPLES_PER_PHASE)["busy"]
    return (
        statistics.median(sample.cpu_ms for sample in samples),
        statistics.median(sample.wall_ms for sample in samples),
    )


def _sensitivity_ceiling(wall_swing: float) -> float:
    """The most CPU time may swing given how far wall time swung beside it."""
    return 1.0 + (wall_swing - 1.0) / LOAD_SENSITIVITY_RATIO


@contextmanager
def _cpu_hogs(
    count: int, commands: Sequence[Sequence[str]] | None = None
) -> Iterator[list[subprocess.Popen[bytes]]]:
    """Competing CPU-bound children, reaped on every exit path.

    A shell spin loop, not a Python one: this has to oversubscribe a box the
    agent fleet is ALREADY hammering, so the hog count is large and each hog has
    to be cheap. Twenty Python interpreters cost a couple of hundred MB on a Mac
    that is usually short of RAM; the same number of `sh` loops cost almost
    nothing and compete for the scheduler just as hard.

    A context manager, and the accumulator is built BEFORE the first spawn, on
    purpose. Spawning into a list comprehension loses every child already started
    if the next Popen raises or the run is interrupted, and what leaks is not an
    idle process: it is an unkillable-looking shell spin loop, on a host this
    test only runs on because it was already short of CPU. Codex flagged the
    comprehension on PR #644 (P2, discussion_r3899739840) and it was right.

    ``commands`` is input DATA, not a seam for a double: every path still runs
    the real ``subprocess.Popen``, real fork/exec, real SIGTERM and real wait.
    It exists so the partial-spawn branch can be provoked by handing the OS a
    program it genuinely cannot exec, after some it genuinely can. Exhausting
    the process table would reach that branch too, and would take the rest of
    the fleet down with it.
    """
    plan = [HOG_COMMAND] * count if commands is None else [list(c) for c in commands]
    hogs: list[subprocess.Popen[bytes]] = []
    try:
        for command in plan:
            # PERF401 wants a comprehension or list.extend here. Both rebuild the
            # list only once the loop finishes, which is exactly the leak this
            # shape exists to close: a Popen that raises halfway would strand
            # every child already spinning.
            hogs.append(  # noqa: PERF401
                subprocess.Popen(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
        yield hogs
    finally:
        for hog in hogs:
            hog.terminate()
        for hog in hogs:
            hog.wait(timeout=10)


def _probe_marker(label: str) -> str:
    """A marker no other process on this host can be carrying.

    Fixed markers cross-contaminate. Codex reproduced it on PR #704
    (discussion_r3903289017) by running two invocations at once: one counted
    five hogs instead of two, and the other's `pgrep` died of the first's
    SIGTERM. That is not hypothetical here - this repo runs pytest under
    `-n auto` locally and several agents share this Mac - so both the count and
    the `pkill` have to be scoped to one invocation. PID alone is not enough:
    PIDs are reused, and two xdist workers of the same run share nothing but
    still need distinct markers, hence the random component too.
    """
    return f"mdt-hog-{label}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def _marked_hog(marker: str) -> list[str]:
    """The production spin loop, tagged so pgrep can find exactly these."""
    return ["/bin/sh", "-c", f"{HOG_LOOP} # {marker}"]


def _alive(marker: str) -> int:
    """How many processes currently carry this marker in their command line."""
    found = subprocess.run(
        ["pgrep", "-f", marker], capture_output=True, text=True, check=False
    )
    if found.returncode not in (0, 1):
        raise RuntimeError(
            f"pgrep failed with {found.returncode}, so this count is not a "
            f"measurement: {found.stderr.strip()}"
        )
    return len([line for line in found.stdout.splitlines() if line.strip()])


def test_real_hogs_spin_inside_the_block_and_are_dead_after_it() -> None:
    """if the reaper does not really work then every later test inherits the load"""
    marker = _probe_marker("lifecycle")
    try:
        with _cpu_hogs(2, [_marked_hog(marker), _marked_hog(marker)]) as hogs:
            # Control for the assertions after the block AND for the pgrep probe
            # itself: real children are running right now, so a later count of
            # zero means they were reaped rather than never started.
            assert all(hog.poll() is None for hog in hogs), (
                "the hogs exited on their own, so nothing here measures termination"
            )
            live = _alive(marker)
            assert live == 2, (
                f"pgrep found {live} of 2 live hogs, so the probe cannot see the "
                "processes this test reasons about"
            )
        assert all(hog.poll() is not None for hog in hogs), (
            "a hog survived the context manager"
        )
        survivors = _alive(marker)
        assert survivors == 0, f"{survivors} marked hogs outlived the block"
    finally:
        subprocess.run(["pkill", "-f", marker], check=False)


def test_a_failed_spawn_reaps_the_hogs_already_spinning() -> None:
    """if a partial spawn leaks then spin loops outlive pytest on a starved host"""
    marker = _probe_marker("partial-spawn")
    plan = [_marked_hog(marker)] * HOGS_BEFORE_THE_FAILURE + [UNSPAWNABLE]
    try:
        # No "baseline is zero" assertion here: the marker is unique to this
        # invocation, so that check could never fail and would only look like
        # diligence. The control this test actually leans on is
        # test_real_hogs_spin_inside_the_block_and_are_dead_after_it, which
        # proves the pgrep probe can SEE marked hogs; without it a survivor
        # count of zero would be indistinguishable from a broken probe.
        with pytest.raises(FileNotFoundError), _cpu_hogs(len(plan), plan):
            pass  # pragma: no cover - the context manager raises on entry

        survivors = _alive(marker)
        assert survivors == 0, (
            f"{survivors} of {HOGS_BEFORE_THE_FAILURE} real shell spin loops were left "
            "running after a mid-spawn failure"
        )
    finally:
        subprocess.run(["pkill", "-f", marker], check=False)


def test_a_stalled_call_is_not_charged_the_stall() -> None:
    """if wall time reaches the verdict then a busy box reads as a slow kernel"""
    sample = time_call(lambda: time.sleep(STALL_S))

    # Control first: prove the stall actually happened, so the cpu_ms assertion
    # below is measuring an instrument rather than a call that never ran.
    assert sample.wall_ms >= STALL_S * 1000 * 0.9, (
        f"the stall did not happen: wall_ms={sample.wall_ms:.1f} for a {STALL_S}s sleep, "
        "so this test proved nothing about the CPU clock"
    )
    assert sample.cpu_ms < STALL_S * 1000 * 0.2, (
        f"cpu_ms={sample.cpu_ms:.1f} charged the process for a {STALL_S}s sleep it slept "
        "through - this is the wall-clock instrument, and it is what made the waveform "
        "bench red whenever the agent fleet was busy"
    )


def test_cpu_time_holds_while_wall_time_swings_with_the_machine() -> None:
    """if the metric tracks machine load then the same commit scores differently"""
    _phase()  # discard: process start and first-touch page faults are not the subject
    quiet_cpu_ms, quiet_wall_ms = _phase()
    with _cpu_hogs(HOG_COUNT) as hogs:
        _phase()  # discard: lets the children reach steady state
        loaded_cpu_ms, loaded_wall_ms = _phase()

    wall_swing = max(quiet_wall_ms, loaded_wall_ms) / min(quiet_wall_ms, loaded_wall_ms)
    cpu_swing = max(quiet_cpu_ms, loaded_cpu_ms) / min(quiet_cpu_ms, loaded_cpu_ms)
    measured = (
        f"cpu {quiet_cpu_ms:.1f} -> {loaded_cpu_ms:.1f}ms ({cpu_swing:.2f}x swing), "
        f"wall {quiet_wall_ms:.1f} -> {loaded_wall_ms:.1f}ms ({wall_swing:.2f}x swing)"
    )
    if wall_swing < WALL_SWING_FLOOR:
        pytest.skip(
            f"UNKNOWN, not a pass: {len(hogs)} CPU hogs moved wall time only "
            f"{wall_swing:.2f}x, under the {WALL_SWING_FLOOR}x this test needs before it "
            f"can claim the machine's conditions changed at all ({measured})"
        )
    ceiling = _sensitivity_ceiling(wall_swing)
    assert cpu_swing <= ceiling, (
        f"process CPU time swung {cpu_swing:.2f}x while wall swung {wall_swing:.2f}x, so it "
        f"is only {(wall_swing - 1.0) / max(cpu_swing - 1.0, 1e-9):.1f}x less load-sensitive, "
        f"under the {LOAD_SENSITIVITY_RATIO}x this fix claims (ceiling {ceiling:.2f}x here). "
        f"The bench verdict tracks fleet load again ({measured})"
    )


@pytest.mark.parametrize(
    ("label", "cpu_swing", "wall_swing", "passes"),
    [
        # Real measurements, so this table is evidence rather than illustration.
        ("4-vCPU ubuntu runner, 4x oversubscribed", 1.87, 9.71, True),
        ("16-core self-hosted runner, 4x oversubscribed", 2.50, 12.08, True),
        ("10-core Mac under fleet load", 1.04, 1.86, True),
        # A wall-clock instrument reads one number and reports it on both sides.
        ("pre-fix instrument, busy CI", 9.71, 9.71, False),
        ("pre-fix instrument, busy Mac", 1.86, 1.86, False),
    ],
)
def test_the_ceiling_admits_measured_cpu_swings_and_refuses_a_wall_clock_one(
    label: str, cpu_swing: float, wall_swing: float, passes: bool
) -> None:
    """if the ceiling needs a per-machine number then it rots between machines"""
    assert (cpu_swing <= _sensitivity_ceiling(wall_swing)) is passes, (
        f"{label}: cpu {cpu_swing}x against wall {wall_swing}x scored "
        f"{'pass' if not passes else 'fail'} at ceiling "
        f"{_sensitivity_ceiling(wall_swing):.2f}x, which is backwards"
    )


def test_every_arm_is_timed_once_per_iteration_in_alternating_order() -> None:
    """if an arm is skipped or always goes first then the medians are not comparable"""
    order: list[str] = []
    calls = {name: (lambda name=name: order.append(name)) for name in ("python", "rust")}

    samples = interleaved_samples(calls, 4)

    assert [len(values) for values in samples.values()] == [4, 4], (
        f"expected 4 samples per arm, got {[len(v) for v in samples.values()]}"
    )
    assert order == [
        "python",
        "rust",
        "rust",
        "python",
        "python",
        "rust",
        "rust",
        "python",
    ], f"arms were not interleaved: {order}"


def test_zero_iterations_is_refused_rather_than_returning_empty_medians() -> None:
    """if an empty run returns silently then statistics.median raises far downstream"""
    with pytest.raises(ValueError, match="at least 1"):
        interleaved_samples({"busy": _busy}, 0)
