"""MIDI clock drift and jitter audit (spec 04 section 5).

Pure: checks a ``ClockMessage`` schedule from ``midi_clock`` against the followed deck's
``BeatEvent`` list from ``beat_clock``. Reports pulses per beat, the worst clock-interval jitter,
overall drift in parts per million, tempo re-anchor points and violations (reversed or duplicate
timestamps, gaps wider than 1.5 tick intervals). No I/O.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from itertools import pairwise

from apps.lighting.beat_clock import BeatEvent
from apps.lighting.midi_clock import NS_PER_MINUTE, PULSES_PER_BEAT, TIMING_CLOCK, ClockMessage

MAX_GAP_TICKS = 1.5


@dataclass(frozen=True)
class ClockAuditReport:
    """Result of auditing one clock schedule; JSON-serialisable via ``to_dict``."""

    ppqn: float
    max_jitter_ns: int
    drift_ppm: float
    max_gap_ticks: float
    reanchor_host_ns: list[int] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        """Plain dict of the report for json.dumps."""
        return asdict(self)


def _interval_ns(bpm: float) -> float:
    return NS_PER_MINUTE / (bpm * PULSES_PER_BEAT)


def audit_clock(messages: Sequence[ClockMessage], beats: Sequence[BeatEvent]) -> ClockAuditReport:
    """Audit Timing Clock spacing in ``messages`` against the tempo of ``beats``."""
    violations: list[str] = []
    for prev, cur in pairwise(messages):
        if cur.at_host_ns < prev.at_host_ns:
            violations.append(f"reversed timestamp at {cur.at_host_ns} after {prev.at_host_ns}")
    clock_times = [m.at_host_ns for m in messages if m.data == bytes((TIMING_CLOCK,))]
    for prev_ns, cur_ns in pairwise(clock_times):
        if cur_ns == prev_ns:
            violations.append(f"duplicate clock timestamp at {cur_ns}")
    ordered = sorted(beats, key=lambda b: b.at_host_ns)
    beat_times = [b.at_host_ns for b in ordered]
    reanchors = [cur.at_host_ns for prev, cur in pairwise(ordered) if cur.bpm != prev.bpm]
    ticks = sorted(set(clock_times))
    max_jitter = 0.0
    max_gap = 0.0
    actual_span = 0
    expected_span = 0.0
    if ordered:
        for prev_ns, cur_ns in pairwise(ticks):
            index = max(0, bisect_right(beat_times, prev_ns) - 1)
            expected = _interval_ns(ordered[index].bpm)
            actual = cur_ns - prev_ns
            max_jitter = max(max_jitter, abs(actual - expected))
            max_gap = max(max_gap, actual / expected)
            actual_span += actual
            expected_span += expected
    if max_gap > MAX_GAP_TICKS:
        violations.append(f"gap of {max_gap:.2f} tick intervals exceeds {MAX_GAP_TICKS}")
    drift_ppm = (actual_span - expected_span) / expected_span * 1e6 if expected_span else 0.0
    ppqn = len(ticks) / len(ordered) if ordered else 0.0
    return ClockAuditReport(
        ppqn=ppqn,
        max_jitter_ns=round(max_jitter),
        drift_ppm=round(drift_ppm, 3),
        max_gap_ticks=round(max_gap, 4),
        reanchor_host_ns=reanchors,
        violations=violations,
    )
