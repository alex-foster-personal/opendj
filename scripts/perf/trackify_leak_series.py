"""PERFMODE-15 Trackify leak series: raw playing samples and quiescent baselines.

ADR-NEW-trackify-leak-kpi-quiescent-baselines: the leak KPI
(`trackify_mode_retained_slope_mb_per_10min`) is the least-squares slope of
footprint baselines sampled with Trackify's workload undone (autoplay off, deck
unloaded, garbage collected). The raw slope over the 15 s samples taken while a
track plays is still computed and recorded
(`trackify_mode_footprint_slope_mb_per_10min`), but it carries the current
track's decoded audio, so it follows track length and does not gate.

Pure: no process, browser or clock access, so every rule here is unit tested
off macOS. `capture_mode_ratios._sample_leak` fills a `LeakSeries`.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from pathlib import Path

from scripts.diagnostics.probe_log_store import _linear_slope_mb_per_hour

RETAINED_SLOPE_KPI = "trackify_mode_retained_slope_mb_per_10min"
RAW_SLOPE_KPI = "trackify_mode_footprint_slope_mb_per_10min"
ADR_REF = "ADR-NEW-trackify-leak-kpi-quiescent-baselines"

# Played seconds between quiescent checkpoints; the first is at 0, the last at
# the run's duration, so a 3600 s run has 13 baselines.
CHECKPOINT_INTERVAL_S = 300
# Footprint samples per checkpoint (the baseline is their median) and the gap
# between them.
CHECKPOINT_SAMPLES = 3
CHECKPOINT_SAMPLE_GAP_S = 2.0


def expected_baseline_count(duration_s: float, interval_s: float = CHECKPOINT_INTERVAL_S) -> int:
    """Checkpoints a run of `duration_s` played seconds takes: 0, interval, ..., duration."""
    if duration_s <= 0 or interval_s <= 0:
        raise ValueError(f"duration and interval must be positive, got {duration_s}, {interval_s}")
    full, remainder = divmod(duration_s, interval_s)
    return int(full) + 1 + (1 if remainder > 0 else 0)


def next_checkpoint_due(previous_due: float, duration_s: float, interval_s: float = CHECKPOINT_INTERVAL_S) -> float:
    """The played time of the checkpoint after `previous_due`; never past the run's end."""
    return min(previous_due + interval_s, duration_s)


def median_baseline_mb(samples_mb: list[float]) -> float:
    if len(samples_mb) != CHECKPOINT_SAMPLES:
        raise ValueError(f"a baseline needs {CHECKPOINT_SAMPLES} samples, got {len(samples_mb)}")
    return statistics.median(samples_mb)


def _slope_mb_per_10min(points: list[tuple[float, float]], label: str) -> float:
    per_hour = _linear_slope_mb_per_hour([t for t, _ in points], [mb for _, mb in points])
    if per_hour is None:
        raise RuntimeError(f"leak capture produced no computable {label} slope from {len(points)} points")
    return per_hour / 6.0


@dataclass
class LeakSeries:
    """Samples against played seconds (wall time minus time spent quiescent)."""

    duration_s: float
    raw: list[tuple[float, float]] = field(default_factory=list)
    baselines: list[tuple[float, float]] = field(default_factory=list)
    quiescent_s: float = 0.0

    def require_complete(self) -> None:
        """Refuse a run whose baselines do not cover its schedule: such a slope is not a measurement."""
        expected = expected_baseline_count(self.duration_s)
        if len(self.baselines) != expected:
            raise RuntimeError(
                f"leak capture took {len(self.baselines)} quiescent baselines, its schedule names {expected}"
            )
        if self.baselines[-1][0] < self.duration_s:
            raise RuntimeError(
                f"last baseline at {self.baselines[-1][0]:.1f} played s, before the run's {self.duration_s} s"
            )

    def retained_slope_mb_per_10min(self) -> float:
        self.require_complete()
        return _slope_mb_per_10min(self.baselines, "retained (baseline)")

    def raw_slope_mb_per_10min(self) -> float:
        return _slope_mb_per_10min(self.raw, "raw")

    def summary(self) -> str:
        base = [mb for _, mb in self.baselines]
        raw = [mb for _, mb in self.raw]
        return (
            f"baselines={len(base)} interval_s={CHECKPOINT_INTERVAL_S} "
            f"baseline_first/min/max/last_mb={base[0]:.1f}/{min(base):.1f}/{max(base):.1f}/{base[-1]:.1f} "
            f"raw_samples={len(raw)} raw_first/min/max/last_mb={raw[0]:.1f}/{min(raw):.1f}/{max(raw):.1f}/{raw[-1]:.1f} "
            f"quiescent_s={self.quiescent_s:.1f}"
        )

    def write_tsv(self, path: Path) -> None:
        lines = ["kind\tplayed_s\tfootprint_mb"]
        lines += [f"raw\t{t:.1f}\t{mb:.1f}" for t, mb in self.raw]
        lines += [f"baseline\t{t:.1f}\t{mb:.1f}" for t, mb in self.baselines]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
