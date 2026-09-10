"""Tests for each UX scenario's multi-KPI COMBINATION and evidence-cohort
logic (capture_id matching, "conclusive BREAKING survives a missing/mismatched
companion"), scored against the REAL shipped docs/perf/kpi-map.json.

Split out of test_kpi_scorecard_scenarios.py (Thu 10 Sep 2026) to keep that
file under the 600-line file-size ratchet, on the seam it already had:
single-required-KPI scenario tests there, multi-required-KPI combination/
cohort tests here.
"""

from __future__ import annotations

import datetime as _dt

from scripts.perf.kpi_scorecard import UNMEASURED, score_scenarios


def _shipped_kpi_map() -> dict:
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    return json.loads((root / "docs" / "perf" / "kpi-map.json").read_text())


def _score_one(sid: str, entries: list[dict]):
    kpi_map = {"scenarios": {sid: _shipped_kpi_map()["scenarios"][sid]}}
    (score,) = score_scenarios(kpi_map, entries, _dt.date(2026, 9, 9))
    return score


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


def test_s2_combines_when_both_required_readings_share_one_capture() -> None:
    """The positive control: three required KPIs whose readings name the SAME
    `capture_id` are evidence of one real press being measured on every axis
    at once, so they combine into a real verdict rather than UNMEASURED."""
    entries = [
        {
            "kpi": "input_to_audible_ms_p99",
            "value": 25,
            "unit": "ms",
            "date": "2026-09-09",
            "machine": "silver",
            "note": "n=200 presses",
            "source": "manual capture",
            "capture_id": "sess-1",
        },
        {
            "kpi": "visual_feedback_ms_p95",
            "value": 10,
            "unit": "ms",
            "date": "2026-09-09",
            "machine": "silver",
            "note": "n=200 presses",
            "source": "manual capture",
            "capture_id": "sess-1",
        },
        {
            "kpi": "visual_feedback_ms_max",
            "value": 16,
            "unit": "ms",
            "date": "2026-09-09",
            "machine": "silver",
            "note": "n=200 presses",
            "source": "manual capture",
            "capture_id": "sess-1",
        },
    ]
    assert _score_one("S2", entries).verdict == "PASS"


def test_s2_max_visual_feedback_past_the_hard_ceiling_is_over_not_pass() -> None:
    """A fresh reviewer round's own reproduction: p95 alone let PASS overstate
    what was proven, since up to 5% of presses could clear 16ms and still
    report a passing p95. Even with a fast p95, a single press whose OWN
    feedback lands past the 16ms hard ceiling must keep the scenario off
    PASS."""
    entries = [
        {
            "kpi": "input_to_audible_ms_p99",
            "value": 25,
            "unit": "ms",
            "date": "2026-09-09",
            "machine": "silver",
            "note": "n=200 presses",
            "source": "manual capture",
            "capture_id": "sess-2",
        },
        {
            "kpi": "visual_feedback_ms_p95",
            "value": 10,
            "unit": "ms",
            "date": "2026-09-09",
            "machine": "silver",
            "note": "n=200 presses, 1 press over ceiling",
            "source": "manual capture",
            "capture_id": "sess-2",
        },
        {
            "kpi": "visual_feedback_ms_max",
            "value": 40,
            "unit": "ms",
            "date": "2026-09-09",
            "machine": "silver",
            "note": "n=200 presses, 1 press over ceiling",
            "source": "manual capture",
            "capture_id": "sess-2",
        },
    ]
    assert _score_one("S2", entries).verdict == "OVER"


def test_s2_cohort_mismatch_between_required_readings_stays_unmeasured() -> None:
    """The reviewer's own reproduction: each required KPI is independently
    replaced by its own newest ledger row, so an audible p99 from one press-
    testing session used to combine with a visual p95 from an unrelated one
    and report a joint experience nobody actually measured happening
    together. Differing `capture_id` values are the disclosed proof of that,
    so the combination must now refuse to score rather than combine."""
    entries = [
        {
            "kpi": "input_to_audible_ms_p99",
            "value": 25,
            "unit": "ms",
            "date": "2026-09-09",
            "machine": "silver",
            "note": "n=200 presses",
            "source": "manual capture",
            "capture_id": "sess-1",
        },
        {
            "kpi": "visual_feedback_ms_p95",
            "value": 10,
            "unit": "ms",
            "date": "2026-09-09",
            "machine": "silver",
            "note": "n=200 presses",
            "source": "manual capture",
            "capture_id": "sess-2",
        },
    ]
    score = _score_one("S2", entries)
    assert score.verdict == UNMEASURED
    assert "evidence cohort" in score.note


def test_s2_conclusive_breaking_survives_a_cohort_mismatch() -> None:
    """A BREAKING reading is unconditionally the floor per the missing-
    companion-KPI carve-out above, and that must hold even when the other
    required KPI names a DIFFERENT capture_id: the audible reading broke on
    its own, real evidence regardless of what else was measured alongside
    it."""
    entries = [
        {
            "kpi": "input_to_audible_ms_p99",
            "value": 100,
            "unit": "ms",
            "date": "2026-09-09",
            "machine": "silver",
            "note": "n=200 presses",
            "source": "manual capture",
            "capture_id": "sess-1",
        },
        {
            "kpi": "visual_feedback_ms_p95",
            "value": 10,
            "unit": "ms",
            "date": "2026-09-09",
            "machine": "silver",
            "note": "n=200 presses",
            "source": "manual capture",
            "capture_id": "sess-2",
        },
    ]
    assert _score_one("S2", entries).verdict == "BREAKING"


def test_s2_stays_unmeasured_when_neither_required_reading_names_a_capture() -> None:
    """A second reviewer round's own reproduction: when EVERY required
    reading omits `capture_id`, the earlier rule saw no DIFFERING pair and
    combined them anyway - an absent capture_id cannot prove a shared
    session, so it must be treated exactly like a mismatch, not like a
    pass."""
    entries = [
        {
            "kpi": "input_to_audible_ms_p99",
            "value": 25,
            "unit": "ms",
            "date": "2026-09-09",
            "machine": "silver",
            "note": "n=200 presses",
            "source": "manual capture",
        },
        {
            "kpi": "visual_feedback_ms_p95",
            "value": 10,
            "unit": "ms",
            "date": "2026-09-09",
            "machine": "silver",
            "note": "n=200 presses",
            "source": "manual capture",
        },
    ]
    score = _score_one("S2", entries)
    assert score.verdict == UNMEASURED
    assert "evidence cohort" in score.note



def test_s2_stays_unmeasured_when_every_capture_id_is_blank() -> None:
    """A fresh reviewer round's own reproduction: `"capture_id": ""` on every
    required reading used to count as one NAMED cohort (the set held one
    member and it was not None), combining unrelated sessions the same way
    an absent field would - an empty string names no session any more than
    a missing key does."""
    entries = [
        {
            "kpi": "input_to_audible_ms_p99",
            "value": 25,
            "unit": "ms",
            "date": "2026-09-09",
            "machine": "silver",
            "note": "n=200 presses",
            "source": "manual capture",
            "capture_id": "",
        },
        {
            "kpi": "visual_feedback_ms_p95",
            "value": 10,
            "unit": "ms",
            "date": "2026-09-09",
            "machine": "silver",
            "note": "n=200 presses",
            "source": "manual capture",
            "capture_id": "",
        },
    ]
    score = _score_one("S2", entries)
    assert score.verdict == UNMEASURED
    assert "evidence cohort" in score.note


def test_s2_non_scalar_capture_id_does_not_crash_the_scorecard() -> None:
    """A fresh reviewer round's own reproduction: a ledger row recording
    `capture_id` as a JSON array or object used to reach a bare `{...}` set
    literal in `_cohort_mismatch_reason` and crash with `TypeError:
    unhashable type`, taking down the whole scorecard for one malformed
    row."""
    entries = [
        {
            "kpi": "input_to_audible_ms_p99",
            "value": 25,
            "unit": "ms",
            "date": "2026-09-09",
            "machine": "silver",
            "note": "n=200 presses",
            "source": "manual capture",
            "capture_id": ["sess-1", "sess-2"],
        },
        {
            "kpi": "visual_feedback_ms_p95",
            "value": 10,
            "unit": "ms",
            "date": "2026-09-09",
            "machine": "silver",
            "note": "n=200 presses",
            "source": "manual capture",
            "capture_id": "sess-1",
        },
    ]
    score = _score_one("S2", entries)
    assert score.verdict == UNMEASURED
    assert "evidence cohort" in score.note
