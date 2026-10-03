"""PERFMODE-15 ratio rows use the shared PERFMODE-14 scorer (median, >=6 floor)."""

from __future__ import annotations

import pytest

from scripts.perf.capture_kpi_ledger import session_meta
from scripts.perf.perfmode14_scorer import (
    PERFMODE14_SCORER,
    _MIN_SCORED_SAMPLES,
    perfmode14_medians_from_lists,
    perfmode14_median,
)
from scripts.perf.mode_ratio_rows import PROCESS_FAMILY, gig_baseline_rows

_IDS = [f"{deck}" * 40 for deck in "abcd"]


def _phase(
    browser_fp: float,
    browser_cpu: float,
    engine_fp: float,
    engine_cpu: float,
    *,
    sample_count: int = _MIN_SCORED_SAMPLES,
    settle_s: float | None = 60.0,
) -> dict[str, float]:
    phase = {
        "footprint_mb": browser_fp + engine_fp,
        "cpu_percent": browser_cpu + engine_cpu,
        "browser_footprint_mb": browser_fp,
        "browser_cpu_percent": browser_cpu,
        "engine_footprint_mb": engine_fp,
        "engine_cpu_percent": engine_cpu,
        "engine_pid_count_max": 1.0,
        "sample_count": float(sample_count),
    }
    if settle_s is not None:
        phase["settle_s"] = float(settle_s)
    return phase


@pytest.mark.requirement("PERFMODE-15")
def test_perfmode14_medians_use_median_not_mean() -> None:
    """[if] footprint and CPU series are scored [then] medians match statistics.median, [else stop]."""
    footprint = [100.0, 200.0, 300.0, 400.0, 500.0, 600.0]
    cpu = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    fp_median, cpu_median = perfmode14_medians_from_lists(
        footprint, cpu, mode="test"
    )
    assert fp_median == perfmode14_median(footprint)
    assert cpu_median == perfmode14_median(cpu)
    assert fp_median == 350.0
    assert cpu_median == 3.5


@pytest.mark.requirement("PERFMODE-15")
def test_perfmode14_scorer_refuses_fewer_than_six_samples() -> None:
    """[if] a phase has fewer than six ticks [then] scoring refuses, [else stop]."""
    with pytest.raises(ValueError, match=f">={_MIN_SCORED_SAMPLES}"):
        perfmode14_medians_from_lists([1.0] * 5, [2.0] * 5, mode="gig")
    with pytest.raises(SystemExit, match=f">={_MIN_SCORED_SAMPLES}"):
        gig_baseline_rows(
            _phase(1300.0, 100.0, 400.0, 5.0, sample_count=5),
            _phase(300.0, 20.0, 400.0, 5.0),
            list(_IDS),
            session_meta(sha="deadbeef"),
        )


@pytest.mark.requirement("PERFMODE-15")
def test_gig_baseline_ratio_rows_carry_perfmode14_scorer() -> None:
    """[if] both phases settle_s>=60 [then] ratio rows carry scorer perfmode14, [else stop]."""
    rows = gig_baseline_rows(
        _phase(1300.0, 100.0, 400.0, 5.0, settle_s=60),
        _phase(300.0, 20.0, 400.0, 5.0, settle_s=60),
        list(_IDS),
        session_meta(sha="deadbeef"),
    )
    assert len(rows) == 2
    for row in rows:
        assert row["process_family"] == PROCESS_FAMILY
        assert row["settle_s"] == 60.0
        assert row["scorer"] == PERFMODE14_SCORER


@pytest.mark.requirement("PERFMODE-15")
def test_gig_baseline_ratio_rows_omit_scorer_when_settle_s_below_floor() -> None:
    """[if] settle_s is below 60 [then] ratio rows omit scorer perfmode14, [else stop]."""
    rows = gig_baseline_rows(
        _phase(1300.0, 100.0, 400.0, 5.0, settle_s=5),
        _phase(300.0, 20.0, 400.0, 5.0, settle_s=5),
        list(_IDS),
        session_meta(sha="deadbeef"),
    )
    for row in rows:
        assert row["settle_s"] == 5.0
        assert "scorer" not in row


@pytest.mark.requirement("PERFMODE-15")
def test_gig_baseline_ratio_rows_omit_scorer_when_settle_s_missing() -> None:
    """[if] either phase omits settle_s [then] ratio rows omit scorer perfmode14, [else stop]."""
    rows = gig_baseline_rows(
        _phase(1300.0, 100.0, 400.0, 5.0, settle_s=None),
        _phase(300.0, 20.0, 400.0, 5.0, settle_s=60),
        list(_IDS),
        session_meta(sha="deadbeef"),
    )
    for row in rows:
        assert "settle_s" not in row
        assert "scorer" not in row
