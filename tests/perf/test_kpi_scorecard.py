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


def _shipped_kpi_map() -> dict:
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    return json.loads((root / "docs" / "perf" / "kpi-map.json").read_text())


def _score_one(sid: str, entries: list[dict]):
    kpi_map = {"scenarios": {sid: _shipped_kpi_map()["scenarios"][sid]}}
    (score,) = score_scenarios(kpi_map, entries, _dt.date(2026, 9, 9))
    return score


def test_s1_stays_unmeasured_on_xruns_alone() -> None:
    """The escape this closes: 0 xruns used to PASS S1 with no silent-output
    signal at all, though PR #856 exists specifically to detect one."""
    entries = [
        {
            "kpi": "xruns_per_playing_hour",
            "value": 0,
            "unit": "events per playing hour",
            "date": "2026-09-09",
        }
    ]
    assert _score_one("S1", entries).verdict == UNMEASURED


def test_s2_stays_unmeasured_on_audible_alone() -> None:
    """The escape this closes: a fast audible p99 used to PASS the whole P0
    scenario with the 16ms visual half never measured."""
    entries = [
        {
            "kpi": "input_to_audible_ms_p99",
            "value": 25,
            "unit": "ms",
            "date": "2026-09-09",
            "machine": "silver",
            "note": "n=200 presses",
            "source": "manual capture",
        }
    ]
    assert _score_one("S2", entries).verdict == UNMEASURED


def test_s4_stays_unmeasured_on_frame_delta_alone() -> None:
    """The escape this closes: a frame-delta p95 that clears the (60Hz-assumed)
    ms band used to PASS/ACCEPTABLE with dropped frames never measured."""
    entries = [
        {
            "kpi": "waveform_frame_delta_ms_p95",
            "value": 10,
            "unit": "ms p95 frame delta",
            "date": "2026-09-09",
        }
    ]
    assert _score_one("S4", entries).verdict == UNMEASURED


def test_s6_slow_but_playable_load_is_over_not_breaking() -> None:
    """The invented-threshold escape this closes: a 26s load past the old 25s
    'breaking' ceiling must read OVER, because the spec's own breaking
    condition is blocked playability, not a duration. Both required KPIs
    present (the duration, and a confirmed-zero playability-blocked reading)
    so the scenario actually reaches a verdict rather than UNMEASURED."""
    entries = [
        {
            "kpi": "deck_load_to_stems_ready_s",
            "value": 26,
            "unit": "s",
            "date": "2026-09-09",
            "source": "manual capture",
        },
        {
            "kpi": "deck_load_blocks_playback_ms",
            "value": 0,
            "unit": "ms blocked while stems load",
            "date": "2026-09-09",
            "source": "manual capture",
        },
    ]
    assert _score_one("S6", entries).verdict == "OVER"


def test_s6_stays_unmeasured_on_duration_alone() -> None:
    """The reviewer's reproduction this closes: a duration reading alone used
    to PASS or OVER the whole scenario even though it cannot establish the
    spec's own prerequisite that the deck was already playable while stems
    loaded - a 10s load could equally mean stems never blocked playback, or
    blocked it for all 10 of those seconds. Neither is distinguishable from
    the duration alone, so the scenario must stay UNMEASURED until the
    playability-blocked companion KPI is also recorded."""
    entries = [
        {
            "kpi": "deck_load_to_stems_ready_s",
            "value": 10,
            "unit": "s",
            "date": "2026-09-09",
            "source": "manual capture",
        }
    ]
    assert _score_one("S6", entries).verdict == UNMEASURED


def test_s7_stays_unmeasured_on_latency_alone() -> None:
    """The escape this closes: a fast keystroke-to-render p95 used to PASS
    even when keystrokes were being dropped, which the spec's own breaking
    cell calls out by name."""
    entries = [
        {
            "kpi": "library_filter_keystroke_ms_p95",
            "value": 40,
            "unit": "ms p95",
            "date": "2026-09-09",
        }
    ]
    assert _score_one("S7", entries).verdict == UNMEASURED


def test_s2_conclusive_breaking_audible_survives_missing_visual() -> None:
    """The reviewer's own reproduction: a 100ms audible p99 is unambiguously
    BREAKING against the 60ms ceiling, and must report BREAKING even though
    the paired visual_feedback_ms_p95 was never recorded. Missing evidence
    about the visual half cannot un-break an audible reading that already,
    conclusively, broke."""
    entries = [
        {
            "kpi": "input_to_audible_ms_p99",
            "value": 100,
            "unit": "ms",
            "date": "2026-09-09",
            "machine": "silver",
            "note": "n=200 presses",
            "source": "manual capture",
        }
    ]
    score = _score_one("S2", entries)
    assert score.verdict == "BREAKING"
    assert "visual_feedback_ms_p95" in score.note


def test_s7_conclusive_breaking_keystroke_drop_survives_missing_latency() -> None:
    """A single dropped keystroke is BREAKING per the spec's own breaking
    cell, and must not vanish into UNMEASURED just because the paired
    latency p95 was never recorded."""
    entries = [
        {
            "kpi": "library_filter_dropped_keystrokes_per_session",
            "value": 1,
            "unit": "dropped keystrokes per session",
            "date": "2026-09-09",
            "source": "manual capture",
        }
    ]
    score = _score_one("S7", entries)
    assert score.verdict == "BREAKING"
    assert "library_filter_keystroke_ms_p95" in score.note


def test_a_reading_in_the_wrong_unit_does_not_score() -> None:
    """The gate this exists for: a required row recorded in seconds against a
    map that declares milliseconds must not reach verdict_for at all - a wrong
    unit is not measured, it is a defect in the reading."""
    kpi_map = {
        "scenarios": {
            "S2": {
                "title": "t",
                "class": "P0",
                "kpis": ["input_to_audible_ms_p95"],
                "required": ["input_to_audible_ms_p95"],
                "missing_kpi": "placeholder",
                **LOWER,
            }
        }
    }
    entries = [{"kpi": "input_to_audible_ms_p95", "value": 25, "unit": "s", "date": "2026-09-09"}]
    (score,) = score_scenarios(kpi_map, entries, _dt.date(2026, 9, 9))
    assert score.verdict == UNMEASURED
    assert "input_to_audible_ms_p95" in score.note


def test_a_reading_in_the_declared_unit_still_scores() -> None:
    """The control for the wrong-unit gate: S5's real shape, unit matched,
    must still reach PASS/ACCEPTABLE rather than being caught by the gate."""
    kpi_map = {
        "scenarios": {
            "S5": {
                "title": "t",
                "class": "P1",
                "kpis": ["packaged_deck_load_total_ms"],
                "required": ["packaged_deck_load_total_ms"],
                "missing_kpi": "placeholder",
                **LOWER,
            }
        }
    }
    entries = [
        {
            "kpi": "packaged_deck_load_total_ms",
            "value": 20,
            "unit": "ms",
            "date": "2026-09-09",
            "source": "manual capture",
        }
    ]
    (score,) = score_scenarios(kpi_map, entries, _dt.date(2026, 9, 9))
    assert score.verdict == "PASS"


def test_a_reading_with_no_source_does_not_score() -> None:
    """The gate this exists for: a reading complete in every other respect
    (right unit, parseable date, and machine/note if it needs provenance)
    but missing `source` must not reach verdict_for - an unsourced number
    cannot be traced back to evidence, the same defect class a superseded or
    wrong-unit row already fails closed on."""
    kpi_map = {
        "scenarios": {
            "S5": {
                "title": "t",
                "class": "P1",
                "kpis": ["packaged_deck_load_total_ms"],
                "required": ["packaged_deck_load_total_ms"],
                "missing_kpi": "placeholder",
                **LOWER,
            }
        }
    }
    entries = [
        {"kpi": "packaged_deck_load_total_ms", "value": 20, "unit": "ms", "date": "2026-09-09"}
    ]
    (score,) = score_scenarios(kpi_map, entries, _dt.date(2026, 9, 9))
    assert score.verdict == UNMEASURED
    assert "packaged_deck_load_total_ms" in score.note


def test_a_reading_with_a_null_source_does_not_score() -> None:
    """A JSON `"source": null` row is a distinct case from a MISSING source
    key: `dict.get("source", "")` only falls back to the default when the key
    is absent, so a present-but-null key reaches `str(None)`, which is the
    truthy string "None". A ledger row must not slip through the source gate
    just because it explicitly nulled the field instead of omitting it."""
    kpi_map = {
        "scenarios": {
            "S5": {
                "title": "t",
                "class": "P1",
                "kpis": ["packaged_deck_load_total_ms"],
                "required": ["packaged_deck_load_total_ms"],
                "missing_kpi": "placeholder",
                **LOWER,
            }
        }
    }
    entries = [
        {
            "kpi": "packaged_deck_load_total_ms",
            "value": 20,
            "unit": "ms",
            "date": "2026-09-09",
            "source": None,
        }
    ]
    (score,) = score_scenarios(kpi_map, entries, _dt.date(2026, 9, 9))
    assert score.verdict == UNMEASURED
    assert "packaged_deck_load_total_ms" in score.note


def test_a_missing_or_malformed_date_fails_closed_not_ageless() -> None:
    """The staleness-gate escape: a required row with no parseable date must
    not silently score with no age, because that lets `--max-stale-days`
    exit 0 on evidence it never actually checked."""
    kpi_map = {
        "scenarios": {
            "S1": {
                "title": "t",
                "class": "P0",
                "kpis": ["k"],
                "required": ["k"],
                "missing_kpi": "placeholder",
                **LOWER,
            }
        }
    }
    entries = [{"kpi": "k", "value": 10, "unit": "ms", "date": "not-a-date"}]
    (score,) = score_scenarios(kpi_map, entries, _dt.date(2026, 9, 9))
    assert score.verdict == UNMEASURED
    assert score.age_days is None


def test_a_future_dated_reading_fails_closed() -> None:
    """A negative age from a future-dated row must not bypass the gate either."""
    kpi_map = {
        "scenarios": {
            "S1": {
                "title": "t",
                "class": "P0",
                "kpis": ["k"],
                "required": ["k"],
                "missing_kpi": "placeholder",
                **LOWER,
            }
        }
    }
    entries = [{"kpi": "k", "value": 10, "unit": "ms", "date": "2099-01-01"}]
    (score,) = score_scenarios(kpi_map, entries, _dt.date(2026, 9, 9))
    assert score.verdict == UNMEASURED


def test_negative_sentinel_reading_is_unmeasured() -> None:
    """The probe convention (docs/perf/learnings/_INDEX.md) returns -1, never
    0, when a subject never appeared. Accepting -1 as a value let every
    lower-is-better KPI report PASS (-1 <= budget) from a probe that measured
    nothing."""
    entries = [{"kpi": "k", "value": -1, "unit": "ms", "date": "2026-09-09"}]
    reading = newest_reading(entries, "k")
    assert reading.value is None
    assert reading.measured is False


def test_non_finite_reading_is_unmeasured() -> None:
    """NaN and +/-inf must not reach verdict_for, which would produce an
    arbitrary OVER/BREAKING result from a value that measured nothing."""
    for bad in (float("nan"), float("inf"), float("-inf")):
        entries = [{"kpi": "k", "value": bad, "unit": "ms", "date": "2026-09-09"}]
        reading = newest_reading(entries, "k")
        assert reading.value is None, f"{bad!r} must not read as a measurement"


def test_zero_is_a_real_measurement_not_a_sentinel() -> None:
    """The control for the sentinel gate: 0 is a legitimate reading (e.g. zero
    dropped keystrokes) and must still score, unlike the -1 sentinel."""
    entries = [{"kpi": "k", "value": 0, "unit": "ms", "date": "2026-09-09"}]
    reading = newest_reading(entries, "k")
    assert reading.value == 0.0
    assert reading.measured is True


def test_reading_retains_machine_and_measurement_note() -> None:
    """The gap this closes: `machine` (tier) and per-row `note` (sampling
    window/denominator) were read off the ledger row and then discarded, so a
    verdict shipped with no way to show which machine or window supports it."""
    entries = [
        {
            "kpi": "k",
            "value": 12,
            "unit": "ms",
            "date": "2026-09-09",
            "machine": "silver",
            "note": "n=6 uncached tracks",
        }
    ]
    reading = newest_reading(entries, "k")
    assert reading.machine == "silver"
    assert reading.note == "n=6 uncached tracks"


def test_json_output_preserves_reading_source_and_score_note() -> None:
    """The `--json` escape: a reader consuming the machine format must see the
    same provenance and caveats the text renderer prints, or a verdict like
    S5's ACCEPTABLE ships without the note that qualifies it."""
    import json as _json
    import subprocess
    import sys as _sys
    from pathlib import Path as _Path

    repo_root = _Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [_sys.executable, "-m", "scripts.perf.kpi_scorecard", "--json", "--today", "2026-09-09"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    )
    payload = _json.loads(result.stdout)
    s5 = next(s for s in payload if s["scenario"] == "S5")
    assert s5["note"], "S5's ACCEPTABLE verdict must carry its caveat in --json output"
    measured_readings = [r for r in s5["readings"] if r["value"] is not None]
    assert measured_readings, "S5 fixture expected at least one measured reading"
    assert all(r["source"] for r in measured_readings)
    assert all(r["machine"] for r in measured_readings), (
        "measured readings must carry the machine tier that supports the verdict"
    )
