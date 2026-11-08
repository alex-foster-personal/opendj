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

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.bench.timing import interleaved_samples

REPO_ROOT = Path(__file__).resolve().parents[2]
#: A stall long enough that no clock resolution question arises.
STALL_S = 0.25
# A fresh interpreter runs no thread but its own, so the process CPU time charged during the
# sleep is the instrument's alone. In-process, a thread another test left running in the same
# fast-tier leg was charged too: 125.2 and 133.3 ms on main's control runs of Sat 7 Nov 18:05Z
# and Sun 8 Nov 00:15Z 2026.
_STALL_IN_A_FRESH_PROCESS = (
    "import json, time\n"
    "from scripts.bench.timing import time_call\n"
    f"sample = time_call(lambda: time.sleep({STALL_S}))\n"
    "print(json.dumps([sample.cpu_ms, sample.wall_ms]))\n"
)


def _stall_in_a_fresh_process() -> tuple[float, float]:
    """(cpu_ms, wall_ms) of time_call around a STALL_S sleep, measured in a new interpreter."""
    out = subprocess.run(
        [sys.executable, "-c", _STALL_IN_A_FRESH_PROCESS],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout
    cpu_ms, wall_ms = json.loads(out)
    return cpu_ms, wall_ms


def test_a_stalled_call_is_not_charged_the_stall() -> None:
    """if wall time reaches the verdict then a busy box reads as a slow kernel"""
    cpu_ms, wall_ms = _stall_in_a_fresh_process()

    # Control first: prove the stall actually happened, so the cpu_ms assertion
    # below is measuring an instrument rather than a call that never ran.
    assert wall_ms >= STALL_S * 1000 * 0.9, (
        f"the stall did not happen: wall_ms={wall_ms:.1f} for a {STALL_S}s sleep, "
        "so this test proved nothing about the CPU clock"
    )
    assert cpu_ms < STALL_S * 1000 * 0.2, (
        f"cpu_ms={cpu_ms:.1f} charged the process for a {STALL_S}s sleep it slept "
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
