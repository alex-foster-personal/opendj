"""Tests for the UX scenario scorecard.

The controls here are derived from the claims the tool makes, per
.claude/rules/verification.md. The two that matter most are negative: a
superseded ledger row must NOT score, and a proxy KPI must NOT score, because
both of those failures produce a PASS, which is the direction nobody checks.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from scripts.perf.kpi_scorecard import (
    UNMEASURED,
    age_in_days,
    drift,
    newest_reading,
    score_scenarios,
    spec_scenario_ids,
    verdict_for,
)

LOWER = {"budget": 30.0, "acceptable": 60.0, "lower_is_better": True}
HIGHER = {"budget": 60.0, "acceptable": 30.0, "lower_is_better": False}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (10.0, "PASS"),
        (30.0, "PASS"),
        (45.0, "ACCEPTABLE"),
        (60.0, "ACCEPTABLE"),
        (61.0, "BREAKING"),
    ],
)
def test_lower_is_better_bands(value: float, expected: str) -> None:
    assert verdict_for(value, LOWER) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (90.0, "PASS"),
        (60.0, "PASS"),
        (45.0, "ACCEPTABLE"),
        (30.0, "ACCEPTABLE"),
        (29.0, "BREAKING"),
    ],
)
def test_higher_is_better_bands(value: float, expected: str) -> None:
    """A frames-per-second KPI must not read BREAKING for being fast."""
    assert verdict_for(value, HIGHER) == expected


def test_newest_wins_over_older() -> None:
    entries = [
        {"kpi": "k", "value": 100, "unit": "ms", "date": "2026-08-01"},
        {"kpi": "k", "value": 10, "unit": "ms", "date": "2026-09-01"},
    ]
    assert newest_reading(entries, "k").value == 10


def test_superseded_row_does_not_score() -> None:
    """The refutation case: a withdrawn number must not keep being quoted."""
    entries = [
        {
            "kpi": "k",
            "value": 0.662,
            "unit": "ratio",
            "date": "2026-09-01",
            "note": "SUPERSEDED, do not cite",
        }
    ]
    reading = newest_reading(entries, "k")
    assert reading.superseded is True
    assert reading.measured is False
    assert reading.value is None


def test_superseded_falls_back_to_the_live_row_beneath_it() -> None:
    entries = [
        {"kpi": "k", "value": 5, "unit": "ms", "date": "2026-08-01"},
        {
            "kpi": "k",
            "value": 9,
            "unit": "ms",
            "date": "2026-09-01",
            "note": "SUPERSEDED by a later re-run",
        },
    ]
    assert newest_reading(entries, "k").value == 5


def test_never_recorded_reads_as_absent_not_zero() -> None:
    reading = newest_reading([], "nothing-here")
    assert reading.value is None
    assert reading.measured is False


def test_scenario_with_a_live_reading_scores() -> None:
    kpi_map = {"scenarios": {"S1": {"title": "t", "class": "P0", "kpis": ["k"], **LOWER}}}
    entries = [{"kpi": "k", "value": 12, "unit": "ms", "date": "2026-09-01"}]
    (score,) = score_scenarios(kpi_map, entries, _dt.date(2026, 9, 9))
    assert score.verdict == "PASS"
    assert score.age_days == 8


def test_proxy_only_scenario_never_scores_even_with_a_passing_reading() -> None:
    """The defect this flag exists for: on Wed 9 Sep 2026 three scenarios read
    PASS off KPIs whose own notes said they measured something else."""
    kpi_map = {
        "scenarios": {
            "S9": {
                "title": "t",
                "class": "P1",
                "kpis": ["k"],
                "proxy_only": True,
                "missing_kpi": "server cost, not time to interactive",
                **LOWER,
            }
        }
    }
    entries = [{"kpi": "k", "value": 0.17, "unit": "s", "date": "2026-09-09"}]
    (score,) = score_scenarios(kpi_map, entries, _dt.date(2026, 9, 9))
    assert score.verdict == UNMEASURED
    assert "PROXY ONLY" in score.note


def test_unmeasured_scenario_carries_its_reason() -> None:
    kpi_map = {
        "scenarios": {
            "S3": {
                "title": "t",
                "class": "P0",
                "kpis": [],
                "missing_kpi": "feature not built",
                **LOWER,
            }
        }
    }
    (score,) = score_scenarios(kpi_map, [], _dt.date(2026, 9, 9))
    assert score.verdict == UNMEASURED
    assert score.note == "feature not built"


def test_spec_ids_are_read_in_table_order_without_duplicates() -> None:
    spec = "| S1 | a |\n| S2 | b |\n| S1 | dup |\ntext\n| S10 | c |\n"
    assert spec_scenario_ids(spec) == ["S1", "S2", "S10"]


def test_drift_reports_a_scenario_the_map_forgot() -> None:
    """A map that silently drops a scenario reports a clean board by having
    fewer checks, which is the failure mode the exit code exists for."""
    unmapped, unknown = drift({"scenarios": {"S1": {}}}, ["S1", "S2"])
    assert unmapped == ["S2"]
    assert unknown == []


def test_drift_reports_a_scenario_the_spec_retired() -> None:
    unmapped, unknown = drift({"scenarios": {"S1": {}, "S99": {}}}, ["S1"])
    assert unmapped == []
    assert unknown == ["S99"]


def test_no_drift_between_the_shipped_map_and_the_shipped_spec() -> None:
    """The live control: the files as they actually ship must agree."""
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    kpi_map = json.loads((root / "docs" / "perf" / "kpi-map.json").read_text())
    ids = spec_scenario_ids((root / "specs" / "perf-latency-program.md").read_text())
    assert ids, (
        "the spec's scenario table parsed as empty, which would make drift detection vacuous"
    )
    assert drift(kpi_map, ids) == ([], [])


def test_age_is_none_for_an_unparseable_date() -> None:
    assert age_in_days("not-a-date", _dt.date(2026, 9, 9)) is None
    assert age_in_days(None, _dt.date(2026, 9, 9)) is None
