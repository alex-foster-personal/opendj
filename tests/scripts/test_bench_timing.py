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

import pytest

from scripts.bench.timing import interleaved_samples, time_call

#: A stall long enough that no clock resolution question arises.
STALL_S = 0.25

#: Rounds of integer work per timed call. Sized for roughly 20-40ms of CPU, which
#: is the same order as the real kernels (16.0ms Python / 7.0ms Rust at 38400
#: points) and far above process_time's resolution.
BUSY_ROUNDS = 200_000

#: Samples per phase. Both instruments are reduced by the SAME statistic, the
#: median, on purpose: it is the statistic the bench itself gates on, and using
#: one reduction for CPU and another for wall would let the two swings differ for
#: a reason that has nothing to do with which clock was read.
SAMPLES_PER_PHASE = 7

#: Competing processes to spawn. Sized to oversubscribe a box that is already
#: busy: a run on an idle Mac needs far fewer, but the machine this defect
#: actually bites on carries a load average in the 40s to 60s, and 2x the core
#: count barely moves wall time there (measured 1.33x, which tripped the UNKNOWN
#: skip below rather than testing anything).
HOG_COUNT = min(6 * (os.cpu_count() or 4), 64)

#: Wall time must move at least this much BETWEEN the two phases, in either
#: direction, or the machine's conditions demonstrably did not change and the
#: CPU-side assertion below would prove nothing. The direction is deliberately
#: not pinned: on a box the agent fleet is already hammering, the quiet phase is
#: routinely the contended one (measured here: wall 417.5 -> 30.5ms while CPU
#: held at 10.6 -> 10.2ms), and a floor that only accepts inflation would call
#: that a failed control when it is the cleanest demonstration of the defect.
WALL_SWING_FLOOR = 1.5

#: CPU time per call is allowed to drift this much under load (cache pressure,
#: core migration and frequency changes are all real; measured 1.04x here with 16
#: hogs on 10 cores), and no further. Kept strictly BELOW WALL_SWING_FLOOR so the
#: two thresholds cannot both be satisfied by one number: a wall-clock instrument
#: reports the same value on both sides, so it necessarily lands above the
#: ceiling once the skip gate above has confirmed wall moved at all. That is what
#: makes this test bite against the pre-fix source instead of merely passing.
CPU_DRIFT_CEILING = 1.35


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


def _spawn_cpu_hogs(count: int) -> list[subprocess.Popen[bytes]]:
    """Competing CPU-bound children. Caller must terminate them.

    A shell spin loop, not a Python one: this has to oversubscribe a box the
    agent fleet is ALREADY hammering, so the hog count is large and each hog has
    to be cheap. Twenty Python interpreters cost a couple of hundred MB on a Mac
    that is usually short of RAM; the same number of `sh` loops cost almost
    nothing and compete for the scheduler just as hard.
    """
    return [
        subprocess.Popen(
            ["/bin/sh", "-c", "while :; do :; done"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(count)
    ]


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
    hogs = _spawn_cpu_hogs(HOG_COUNT)
    try:
        _phase()  # discard: lets the children reach steady state
        loaded_cpu_ms, loaded_wall_ms = _phase()
    finally:
        for hog in hogs:
            hog.terminate()
        for hog in hogs:
            hog.wait(timeout=10)

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
    assert cpu_swing <= CPU_DRIFT_CEILING, (
        f"process CPU time swung {cpu_swing:.2f}x with the machine, over the "
        f"{CPU_DRIFT_CEILING}x ceiling, so the bench verdict tracks fleet load again "
        f"({measured})"
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
