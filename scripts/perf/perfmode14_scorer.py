"""PERFMODE-14 scoring helpers (median aggregation and absolute sample floor).

Shared by library-mode capture, Trackify ratio capture, and ledger row builders.
"""

from __future__ import annotations

import statistics

# Codex P1/BLOCKING, PR #4034, discussion_r4138153190: the Playwright spec's
# own floor (library-mode-perf-capture.spec.ts's MIN_SAMPLE_FRACTION) is
# relative to `expectedTicks`, which collapses to 1 when
# KPI_CAPTURE_SAMPLE_INTERVAL_S is set to the full dwell length or longer --
# a single reading would then satisfy that floor and could still flip
# LIB-MODE/PERFMODE-14 to PASS without establishing steady-state behavior.
# This is an ABSOLUTE floor, independent of whatever interval produced the
# samples: at the documented default (60s dwell, 5s interval) a conforming
# capture yields about 12 ticks per mode, so 6 is a conservative minimum that
# only a materially widened interval (or heavy sample failures) can miss.
_MIN_SCORED_SAMPLES = 6

# Ledger rows scored with the PERFMODE-14 sample floor and median aggregation
# carry this marker (PERFMODE-15 ratio clauses read only rows with it).
PERFMODE14_SCORER = "perfmode14"


def perfmode14_median(samples: list[float]) -> float:
    """Median of one dwell's samples (matches library-mode-perf-capture.spec.ts)."""
    if not samples:
        raise ValueError("median requires at least one sample")
    return float(statistics.median(samples))


def perfmode14_require_scored_sample_count(count: int, *, mode: str, field: str) -> None:
    """Fail fast when a scored field's tick count is below the absolute floor."""
    if count < _MIN_SCORED_SAMPLES:
        raise ValueError(
            f"{mode} {field} has {count} sample(s); "
            f"PERFMODE-14 requires >={_MIN_SCORED_SAMPLES}"
        )


def perfmode14_require_scored_samples(
    samples: list[float], *, mode: str, field: str
) -> None:
    """Fail fast when a mode's scored field is empty or below the absolute floor."""
    if not isinstance(samples, list) or len(samples) == 0:
        raise ValueError(f"{mode} {field} holds no samples; a median over none is not a capture")
    perfmode14_require_scored_sample_count(len(samples), mode=mode, field=field)


def perfmode14_medians_from_lists(
    footprint_samples_mb: list[float],
    cpu_samples_percent: list[float],
    *,
    mode: str,
) -> tuple[float, float]:
    """Median footprint and ps-based CPU percent after the PERFMODE-14 sample floor."""
    perfmode14_require_scored_samples(footprint_samples_mb, mode=mode, field="footprint_samples_mb")
    perfmode14_require_scored_samples(cpu_samples_percent, mode=mode, field="cpu_samples_percent")
    return (
        perfmode14_median(footprint_samples_mb),
        perfmode14_median(cpu_samples_percent),
    )
