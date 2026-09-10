"""Tests for the scorecard's own primitives: verdict bands (`verdict_for`),
reading selection (`newest_reading`), and age parsing (`age_in_days`) -
against synthetic fixture maps, not the real shipped kpi-map.json.

The controls here are derived from the claims the tool makes, per
.claude/rules/verification.md. The two that matter most are negative: a
superseded ledger row must NOT score, and a proxy KPI must NOT score, because
both of those failures produce a PASS, which is the direction nobody checks.

Per-scenario combination/cohort tests against the real shipped map live in
test_kpi_scorecard_scenarios.py (split out Thu 10 Sep 2026 to keep this file
under the 600-line file-size ratchet); per-reading validity tests (unit,
source, date, sentinel values) live in test_kpi_scorecard_reading_validation.py.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from scripts.perf.kpi_scorecard import (
    UNMEASURED,
    age_in_days,
    newest_reading,
    score_scenarios,
    verdict_for,
)

LOWER = {
    "budget": 30.0,
    "acceptable": 60.0,
    "breaking": 100.0,
    "lower_is_better": True,
    "unit": "ms",
}
HIGHER = {"budget": 60.0, "acceptable": 30.0, "breaking": 10.0, "lower_is_better": False}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (10.0, "PASS"),
        (30.0, "PASS"),
        (45.0, "ACCEPTABLE"),
        (60.0, "ACCEPTABLE"),
        (61.0, "OVER"),
        (100.0, "OVER"),
        (101.0, "BREAKING"),
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
        (29.0, "OVER"),
        (10.0, "OVER"),
        (9.0, "BREAKING"),
    ],
)
def test_higher_is_better_bands(value: float, expected: str) -> None:
    """A frames-per-second KPI must not read BREAKING for being fast."""
    assert verdict_for(value, HIGHER) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.0, "PASS"),
        (4.99, "ACCEPTABLE"),
        (5.0, "OVER"),
        (5.01, "OVER"),
    ],
)
def test_acceptable_strict_excludes_the_boundary_value(value: float, expected: str) -> None:
    """S4's own reproduction: a spec cell stated as "<5%" with no equals sign
    must report OVER, not ACCEPTABLE, for a reading of exactly 5%. The default
    inclusive `<=` (test_lower_is_better_bands above) would wrongly call this
    ACCEPTABLE; `acceptable_strict` is what closes that gap."""
    cfg = {"budget": 0.0, "acceptable": 5.0, "breaking": None, "lower_is_better": True,
           "acceptable_strict": True}
    assert verdict_for(value, cfg) == expected


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


def test_a_null_date_does_not_crash_the_sort() -> None:
    """The reviewer's own reproduction: a ledger row recorded with
    `"date": null` used to raise `TypeError: '<' not supported between
    instances of 'NoneType' and 'str'` the moment it sorted against a sibling
    row with a real date string, crashing the entire scorecard rather than
    being caught by score_scenarios's own malformed-date rejection - that
    logic never runs, because the crash happens earlier, during selection."""
    entries = [
        {"kpi": "k", "value": 100, "unit": "ms", "date": None},
        {"kpi": "k", "value": 10, "unit": "ms", "date": "2026-09-01"},
    ]
    assert newest_reading(entries, "k").value == 10


def test_never_recorded_reads_as_absent_not_zero() -> None:
    reading = newest_reading([], "nothing-here")
    assert reading.value is None
    assert reading.measured is False


def test_scenario_with_a_live_reading_scores() -> None:
    kpi_map = {
        "scenarios": {
            "S1": {"title": "t", "class": "P0", "kpis": ["k"], "required": ["k"], **LOWER}
        }
    }
    entries = [
        {"kpi": "k", "value": 12, "unit": "ms", "date": "2026-09-01", "source": "manual capture"}
    ]
    (score,) = score_scenarios(kpi_map, entries, _dt.date(2026, 9, 9))
    assert score.verdict == "PASS"
    assert score.age_days == 8


def test_a_context_kpi_alone_never_scores_a_scenario() -> None:
    """A reviewer's case: with an any-measured test, a scenario reports PASS on
    an adjacent reading while the KPI that proves it is still missing."""
    kpi_map = {
        "scenarios": {
            "S9": {
                "title": "t",
                "class": "P1",
                "kpis": ["walk_time", "time_to_interactive"],
                "required": ["time_to_interactive"],
                "missing_kpi": "server cost, not time to interactive",
                **LOWER,
            }
        }
    }
    entries = [{"kpi": "walk_time", "value": 0.17, "unit": "s", "date": "2026-09-09"}]
    (score,) = score_scenarios(kpi_map, entries, _dt.date(2026, 9, 9))
    assert score.verdict == UNMEASURED
    assert "time_to_interactive" in score.note


def test_a_partially_measured_scenario_does_not_score() -> None:
    """S2's shape: press-to-schedule recorded, press-to-AUDIBLE still missing."""
    kpi_map = {
        "scenarios": {
            "S2": {
                "title": "t",
                "class": "P0",
                "kpis": ["press_to_schedule_ms_p95", "input_to_audible_ms_p95"],
                "required": ["input_to_audible_ms_p95"],
                "missing_kpi": "press-to-audible is the point of this scenario",
                **LOWER,
            }
        }
    }
    entries = [{"kpi": "press_to_schedule_ms_p95", "value": 2, "unit": "ms", "date": "2026-09-09"}]
    (score,) = score_scenarios(kpi_map, entries, _dt.date(2026, 9, 9))
    assert score.verdict == UNMEASURED


def test_a_null_breaking_threshold_never_reports_breaking() -> None:
    """The spec declines to define one for S10; none may be invented here."""
    cfg = {"budget": 1.0, "acceptable": 2.0, "breaking": None, "lower_is_better": True}
    assert verdict_for(99.0, cfg) == "OVER"


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


def test_age_is_none_for_an_unparseable_date() -> None:
    assert age_in_days("not-a-date", _dt.date(2026, 9, 9)) is None
    assert age_in_days(None, _dt.date(2026, 9, 9)) is None


