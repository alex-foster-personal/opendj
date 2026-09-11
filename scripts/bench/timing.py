"""Load-immune timing primitives for the CPU-bound benchmarks in this package.

WHY THIS EXISTS. A benchmark of a CPU-bound kernel that gates on ELAPSED WALL
time is not measuring the kernel, it is measuring the kernel plus every other
process that ran while the kernel was descheduled. On a box shared with an agent
fleet that second term dominates and moves hour to hour, so the same commit
scores differently every run and the gate reds for reasons the diff cannot
explain. ``scripts/bench/waveform_materialization.py`` reported 6.28, 14.29 and
33.53 percent improvement on ONE commit while the box's load average climbed
from 42 to 68 on Mon 31 Aug 2026.

THE INSTRUMENT. ``time.process_time_ns`` counts user+system CPU consumed by THIS
process across all its threads, and nothing else. Time the process spent
runnable-but-not-running is not charged to it, which is the property a CPU-cost
gate needs.

WHAT IT IS NOT: perfectly load-invariant. CPU time measures time ON the CPU, and
contention makes the same instruction stream genuinely occupy the CPU longer
through shared-cache thrashing and SMT siblings. Measured on a 4-vCPU
ubuntu-latest runner under 6x oversubscription, one fixed workload moved 12.3 ->
22.9ms of CPU (1.87x) while its wall time moved 12.3 -> 119.3ms (9.71x). So the
honest claim is that this instrument is roughly an ORDER OF MAGNITUDE less
load-sensitive than elapsed time, not that it is immune, and the regression test
pins that relative claim rather than an absolute drift number it would have to
keep re-tuning per machine.

WHAT IT DOES NOT MEASURE, stated so nobody re-gates on the wrong half:

- Wall time is still collected, and is still the right instrument for anything
  whose subject IS concurrency (thread throughput, request rates, a pipeline
  stall). Those are report-only here precisely because they track machine load.
- CPU time counts every thread of this process. A kernel that fans work out
  across threads reports the SUM of that work, not its wall duration, which is
  the honest cost but is not the latency an operator feels.
- CPU time does not charge a child process. A benchmark that shells out must
  measure the child itself.

Acceptance tests (tests/scripts/test_bench_timing.py):
  [if] a call sleeps rather than computes
       [then] its cpu_ms is near zero while its wall_ms is the full stall
  [if] the box is loaded with competing CPU hogs
       [then] cpu_ms holds while wall_ms inflates
  [if] an arm is timed N times
       [then] it was called exactly N times and the arm order alternated
"""

from __future__ import annotations

import gc
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Sample:
    """One timed call, in milliseconds, on both instruments.

    ``cpu_ms`` is the gate-grade number. ``wall_ms`` is kept alongside it so a
    report can show how much contention the run met without that contention
    reaching the verdict.
    """

    cpu_ms: float
    wall_ms: float


def time_call(call: Callable[[], object]) -> Sample:
    """Time one call on process CPU time and on wall time."""
    cpu_started = time.process_time_ns()
    wall_started = time.perf_counter_ns()
    call()
    cpu_ns = time.process_time_ns() - cpu_started
    wall_ns = time.perf_counter_ns() - wall_started
    return Sample(cpu_ms=cpu_ns / 1_000_000, wall_ms=wall_ns / 1_000_000)


def interleaved_samples(
    calls: Mapping[str, Callable[[], object]], iterations: int
) -> dict[str, list[Sample]]:
    """Time every arm once per iteration, alternating which arm goes first.

    Interleaving is what keeps a slow stretch of the machine from landing on one
    arm only. It matters less now that the verdict reads CPU time, and it is kept
    because the wall figures reported beside it are still comparable only if both
    arms met the same conditions.

    The collector disables the cyclic garbage collector for the duration so a
    collection triggered by arm A is not charged to arm B.
    """
    if iterations < 1:
        raise ValueError(f"iterations must be at least 1, got {iterations}")
    names = list(calls)
    samples: dict[str, list[Sample]] = {name: [] for name in names}
    gc.disable()
    try:
        for iteration in range(iterations):
            order = names if iteration % 2 == 0 else list(reversed(names))
            for name in order:
                samples[name].append(time_call(calls[name]))
    finally:
        gc.enable()
    return samples
