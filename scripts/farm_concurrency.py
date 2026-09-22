#!/usr/bin/env python3
"""Observed container concurrency, reconstructed from per-call wall spans.

THE BUG THIS EXISTS TO KILL: ``scripts/bench/kpi_ledger.json`` recorded
``max_parallel_gpus: 10`` for every Modal snapshot, under a KPI defined as
"Most GPUs the farm ran concurrently during the run". Nothing measured it. The
value was ``DEFAULT_MAX_CONTAINERS``, the CONFIGURED cap, copied into a
measurement slot -- and the same 10 was recorded for a ONE-TRACK run, where 10
is arithmetically impossible. It sat there for seven snapshots and was used to
size the backlog.

A config value in a measurement slot is worse than a missing number, because it
is unfalsifiable by inspection: it looks like evidence. So this module derives
concurrency ONLY from timestamps stamped inside the containers that ran the
calls, and imports nothing from the farm.

Kept separate from ``scripts/modal_vocal_farm.py`` so it is testable without
the ``modal`` package, which is overlaid at run time rather than being a repo
dependency.

  ✔︎ ✅ 🎯 peak is the largest number of calls overlapping at any instant.
    [if] two calls do not overlap [then] peak is 1, not 2
    [if] one call ends exactly as another starts [then] peak is 1
  ✔︎ ✅ 🎯 mean is container-seconds over the busy window, so it approaches the
    cap under a true fan-out and 1.0 when calls are serialised.
    [if] 10 identical calls run simultaneously [then] mean is 10.0
    [if] 10 identical calls run back to back [then] mean is 1.0
  ✔︎ ✅ 🎯 a span that ends before it starts is a clock fault, not a datum.
    [if] end < start [then ⛔️] raise rather than report a negative overlap

-Claude
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

# What a container must return for its occupancy to be measurable at all.
SPAN_KEY: str = "wall_span"


def concurrency_profile(spans: Sequence[tuple[float, float]]) -> dict[str, float]:
    """Peak and mean concurrency over ``[start, end]`` epoch-second spans.

    ``peak`` is a sweep line: the largest number of calls whose intervals
    overlapped at any instant. Ties close before they open, so a call ending
    exactly as another starts is never counted as an overlap.

    ``mean`` is time-weighted over the busy window (summed container seconds
    divided by the wall clock from the first start to the last end). That is
    the number to compare against the configured cap: a true 10-way fan-out
    approaches 10. The 128-track production run measured 5.34 (954.1s of
    container time over a 178.5s publication window, 118 entries carrying
    telemetry). An earlier 1.05 figure for that run was RETRACTED: it divided
    by a hand-entered ledger field rather than by measured wall, and 954.1s of
    container time inside a 178.5s window cannot yield 1.05.

    The one assumption is that container clocks agree. Modal's hosts are
    NTP-synced; a skew of s seconds perturbs any single overlap by at most s,
    immaterial against multi-second GPU calls but not against short ones.
    """
    if not spans:
        return {"peak": 0.0, "mean": 0.0, "busy_window_s": 0.0, "calls": 0.0}
    for start, end in spans:
        if end < start:
            raise ValueError(f"span ends before it starts: [{start}, {end}]")
    events = sorted(
        [(start, 1) for start, _ in spans] + [(end, -1) for _, end in spans]
    )
    running = 0
    peak = 0
    for _, delta in events:
        running += delta
        peak = max(peak, running)
    busy_window_s = max(end for _, end in spans) - min(start for start, _ in spans)
    occupied_s = sum(end - start for start, end in spans)
    return {
        "peak": float(peak),
        "mean": round(occupied_s / busy_window_s, 2) if busy_window_s > 0 else 0.0,
        "busy_window_s": round(busy_window_s, 1),
        "calls": float(len(spans)),
    }


def span_of(result: dict[str, Any]) -> tuple[float, float]:
    """One call's occupancy window, or a hard failure.

    A missing span means the container is running a build older than this
    instrumentation. Skipping it quietly would understate concurrency, which is
    precisely the class of quiet-wrong number this module exists to prevent, so
    it raises instead.
    """
    span = result.get(SPAN_KEY)
    if span is None:
        raise RuntimeError(
            f"result for {result.get('stable_id')!r} carries no {SPAN_KEY}; the "
            "container is running a build older than the concurrency "
            "instrumentation, so observed concurrency cannot be measured."
        )
    if len(span) != 2:
        raise RuntimeError(f"malformed {SPAN_KEY} {span!r}: expected [start, end]")
    return float(span[0]), float(span[1])


def implied_max_containers(items_per_s: float, container_s: float) -> float:
    """Containers a feeder emitting ``items_per_s`` can keep busy.

    The ceiling Little's law puts on fan-out: if each call occupies a container
    for ``container_s`` and inputs arrive every ``1 / items_per_s`` seconds,
    no more than the product can ever be in flight, whatever the cap is set to.
    """
    if items_per_s < 0 or container_s < 0:
        raise ValueError(
            f"negative rate or duration: {items_per_s=}, {container_s=}"
        )
    return round(items_per_s * container_s, 2)


def summarise(spans: Iterable[tuple[float, float]], configured_cap: int) -> str:
    """One line contrasting what was OBSERVED with what was configured."""
    seen = concurrency_profile(list(spans))
    return (
        f"observed peak {seen['peak']:.0f} containers, mean {seen['mean']:.2f} "
        f"over a {seen['busy_window_s']:.0f}s busy window "
        f"({seen['calls']:.0f} calls, cap configured at {configured_cap})"
    )
