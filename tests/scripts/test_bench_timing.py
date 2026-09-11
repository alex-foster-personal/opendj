"""The bench timing instrument must not charge a process for time it did not run.

Regression cover for a false red that cost every local agent time whenever the
fleet was busy: ``scripts/bench/waveform_materialization.py`` gated on elapsed
wall time, so one commit scored 6.28, 14.29 and 33.53 percent improvement across
three runs while this box's load average climbed 42 -> 68 on Mon 31 Aug 2026.

The sleep case is the regression guard. It is deterministic, needs no scheduler
cooperation, and fails outright against the wall-clock instrument. A CPU-to-wall
ratio under synthetic host load is not a valid invariant: CPU time may genuinely
increase because cache and SMT contention make the same work consume more CPU.

The regression test was run against the pre-fix instrument before being trusted (``time_call``
temporarily reading ``perf_counter_ns`` into ``cpu_ms``); a test that has not
been shown to go red is not cover.
"""

from __future__ import annotations

import time

import pytest

from scripts.bench.timing import interleaved_samples, time_call

#: A stall long enough that no clock resolution question arises.
STALL_S = 0.25


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
        interleaved_samples({"busy": lambda: None}, 0)
