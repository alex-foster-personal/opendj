"""PERFMODE-15 retained-slope rules (ADR-NEW-trackify-leak-kpi-quiescent-baselines).

Pure series math, so it runs on every host. These are the leak and
working-set controls; test_capture_mode_ratios_leak.py drives the real
browser helper through real checkpoints against a live frontend.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.perf import trackify_leak_series as tls
from scripts.perf.capture_kpi_ledger import session_meta
from scripts.perf.capture_mode_ratios import _leak_rows

_HOUR = 3600.0


def _series(baseline_mb_at, raw_mb_at, duration_s: float = _HOUR) -> tls.LeakSeries:
    """Baselines on the ADR's schedule, raw samples every 15 s between them."""
    series = tls.LeakSeries(duration_s=duration_s)
    due = 0.0
    while True:
        series.baselines.append((due, baseline_mb_at(due)))
        if due >= duration_s:
            break
        due = tls.next_checkpoint_due(due, duration_s)
    series.raw = [(t, raw_mb_at(t)) for t in (15.0 * i for i in range(1, int(duration_s // 15) + 1))]
    return series


def _track_working_set_mb(played_s: float) -> float:
    """Round 4's shape: tracks get longer through the hour, each costs ~0.72 MB per s of its length."""
    track_length_s = 150.0 + played_s / 10.0
    return 300.0 + 0.72 * track_length_s


@pytest.mark.requirement("PERFMODE-15")
def test_a_growing_working_set_with_flat_baselines_is_not_a_leak() -> None:
    """[if] only the playing track's working set grows [then] the retained slope stays under budget while the raw slope does not, [else stop]."""
    series = _series(baseline_mb_at=lambda _t: 120.0, raw_mb_at=_track_working_set_mb)
    assert series.retained_slope_mb_per_10min() == pytest.approx(0.0, abs=0.01)
    assert series.raw_slope_mb_per_10min() > 5.0, "control: the raw slope must read this run as over budget"


@pytest.mark.requirement("PERFMODE-15")
def test_baselines_that_keep_rising_read_as_a_leak() -> None:
    """[if] quiescent baselines rise 1 MB a minute [then] the retained slope reads 10 MB per 10 min, [else stop]."""
    series = _series(baseline_mb_at=lambda t: 120.0 + t / 60.0, raw_mb_at=lambda _t: 400.0)
    assert series.retained_slope_mb_per_10min() == pytest.approx(10.0, abs=0.05)


@pytest.mark.requirement("PERFMODE-15")
def test_a_run_short_of_its_checkpoint_schedule_writes_no_slope() -> None:
    """[if] a baseline is missing [then] the retained slope refuses, [else stop]."""
    series = _series(baseline_mb_at=lambda _t: 120.0, raw_mb_at=lambda _t: 400.0)
    series.baselines.pop()
    with pytest.raises(RuntimeError, match="took 12 quiescent baselines, its schedule names 13"):
        series.retained_slope_mb_per_10min()


@pytest.mark.requirement("PERFMODE-15")
def test_the_schedule_covers_played_time_zero_through_the_end() -> None:
    """[if] a 1 h run is scheduled [then] it has 13 baselines, the last at 3600 s, [else stop]."""
    assert tls.expected_baseline_count(3600) == 13
    assert tls.expected_baseline_count(3650) == 14
    assert tls.next_checkpoint_due(3300.0, 3650.0) == 3600.0
    assert tls.next_checkpoint_due(3600.0, 3650.0) == 3650.0
    series = _series(baseline_mb_at=lambda _t: 1.0, raw_mb_at=lambda _t: 1.0, duration_s=3650.0)
    series.require_complete()
    assert series.baselines[-1][0] == 3650.0


@pytest.mark.requirement("PERFMODE-15")
def test_a_baseline_is_the_median_of_its_samples() -> None:
    """[if] one checkpoint sample spikes [then] the median ignores it, and a wrong sample count refuses, [else stop]."""
    assert tls.median_baseline_mb([100.0, 400.0, 101.0]) == 101.0
    with pytest.raises(ValueError, match="needs 3 samples, got 2"):
        tls.median_baseline_mb([100.0, 101.0])


@pytest.mark.requirement("PERFMODE-15")
def test_a_leak_run_writes_the_gating_retained_row_and_the_raw_row_alongside(tmp_path: Path) -> None:
    """[if] a leak run is recorded [then] both slopes are ledger rows and the series TSV keeps every sample, [else stop]."""
    series = _series(baseline_mb_at=lambda _t: 120.0, raw_mb_at=_track_working_set_mb)
    rows = _leak_rows(series, session_meta(sha="deadbeef"))
    by_kpi = {row["kpi"]: row for row in rows}
    assert set(by_kpi) == {tls.RETAINED_SLOPE_KPI, tls.RAW_SLOPE_KPI}
    assert by_kpi[tls.RETAINED_SLOPE_KPI]["value"] == pytest.approx(0.0, abs=0.01)
    assert by_kpi[tls.RAW_SLOPE_KPI]["value"] > 5.0
    assert all(row["measured"] is True for row in rows)
    assert "does not gate" in by_kpi[tls.RAW_SLOPE_KPI]["note"]
    tsv = tmp_path / "series.tsv"
    series.write_tsv(tsv)
    lines = tsv.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "kind\tplayed_s\tfootprint_mb"
    assert sum(line.startswith("baseline\t") for line in lines) == 13
    assert sum(line.startswith("raw\t") for line in lines) == 240
