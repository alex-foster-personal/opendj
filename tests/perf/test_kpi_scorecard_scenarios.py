"""Tests for each UX scenario's own combination/cohort logic, scored against
the REAL shipped docs/perf/kpi-map.json - distinct from the generic
verdict_for/newest_reading/age unit tests in test_kpi_scorecard.py, which use
synthetic fixture maps.

Split out of test_kpi_scorecard.py (Thu 10 Sep 2026) to keep that file under
the 600-line file-size ratchet, on the seam the original file already had:
generic band/reading-primitive tests above `_shipped_kpi_map`/`_score_one`,
per-scenario combination tests against the real map below it.
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


def test_s4_single_freeze_hidden_in_p95_is_still_breaking_via_max() -> None:
    """A second reviewer round's own reproduction: a lone one-second freeze in
    a 10s@60Hz capture is one slow sample among ~600, far below the rank p95
    actually reads, so a `breaking` bound on the p95 KPI (the first attempt at
    this fix) never fires - hundreds of healthy frames dilute the freeze away.
    `waveform_frame_delta_ms_max` cannot be diluted the same way: one freeze
    IS the max regardless of how many healthy frames surround it. p95 itself
    stays near budget here, proving this is not just a bigger-number version
    of the earlier (wrong) fix."""
    entries = [
        {
            "kpi": "waveform_frame_delta_ms_p95",
            "value": 16.7,
            "unit": "ms p95 frame delta",
            "date": "2026-09-09",
            "machine": "silver",
            "note": "n=10s capture window, 1 of 600 frames stalled",
            "source": "manual capture",
        },
        {
            "kpi": "waveform_dropped_frame_pct_10s",
            "value": 0.17,
            "unit": "% dropped frames per 10s window",
            "date": "2026-09-09",
            "machine": "silver",
            "note": "n=10s capture window, 1 of 600 frames stalled",
            "source": "manual capture",
        },
        {
            "kpi": "waveform_frame_delta_ms_max",
            "value": 1500,
            "unit": "ms max frame delta per 10s window",
            "date": "2026-09-09",
            "machine": "silver",
            "note": "n=10s capture window, 1 of 600 frames stalled",
            "source": "manual capture",
        },
    ]
    assert _score_one("S4", entries).verdict == "BREAKING"


def test_s4_max_frame_gap_at_the_breaking_boundary_is_pass_not_breaking() -> None:
    """The boundary control for the fix above: exactly 1000ms is not yet
    'greater than' the breaking bound (verdict_for's own strict `>` for a
    lower-is-better KPI), and max's zero-width band (budget = acceptable =
    breaking = 1000) means anything at or below it is PASS outright - proving
    the bound lands where intended rather than one tick permissive."""
    entries = [
        {
            "kpi": "waveform_frame_delta_ms_p95",
            "value": 10,
            "unit": "ms p95 frame delta",
            "date": "2026-09-09",
            "machine": "silver",
            "note": "n=10s capture window",
            "source": "manual capture",
            "capture_id": "s4-sess-1",
        },
        {
            "kpi": "waveform_dropped_frame_pct_10s",
            "value": 0,
            "unit": "% dropped frames per 10s window",
            "date": "2026-09-09",
            "machine": "silver",
            "note": "n=10s capture window",
            "source": "manual capture",
            "capture_id": "s4-sess-1",
        },
        {
            "kpi": "waveform_frame_delta_ms_max",
            "value": 1000,
            "unit": "ms max frame delta per 10s window",
            "date": "2026-09-09",
            "machine": "silver",
            "note": "n=10s capture window",
            "source": "manual capture",
            "capture_id": "s4-sess-1",
        },
    ]
    assert _score_one("S4", entries).verdict == "PASS"


def test_s4_high_dropped_frame_pct_alone_is_over_not_breaking() -> None:
    """A high dropped-frame percentage by itself must not read BREAKING: a
    10-second aggregate percentage cannot tell a genuine multi-second stall
    apart from many small, evenly-scattered stutters that sum to the same
    figure, which is exactly why breaking was derived onto
    waveform_frame_delta_ms_max (the single worst gap, not an aggregate)
    rather than onto this KPI - see the kpi-map.json missing_kpi note for
    S4."""
    entries = [
        {
            "kpi": "waveform_frame_delta_ms_p95",
            "value": 20,
            "unit": "ms p95 frame delta",
            "date": "2026-09-09",
            "machine": "silver",
            "note": "n=10s capture window",
            "source": "manual capture",
            "capture_id": "s4-sess-2",
        },
        {
            "kpi": "waveform_dropped_frame_pct_10s",
            "value": 80,
            "unit": "% dropped frames per 10s window",
            "date": "2026-09-09",
            "machine": "silver",
            "note": "n=10s capture window",
            "source": "manual capture",
            "capture_id": "s4-sess-2",
        },
        {
            "kpi": "waveform_frame_delta_ms_max",
            "value": 50,
            "unit": "ms max frame delta per 10s window",
            "date": "2026-09-09",
            "machine": "silver",
            "note": "n=10s capture window",
            "source": "manual capture",
            "capture_id": "s4-sess-2",
        },
    ]
    assert _score_one("S4", entries).verdict == "OVER"


def test_s6_slow_but_playable_load_is_over_not_breaking() -> None:
    """The invented-threshold escape this closes: a 26s load past the old 25s
    'breaking' ceiling must read OVER, because the spec's own breaking
    condition is blocked playability, not a duration. Both required KPIs
    present (the duration, and a confirmed-zero playability-blocked reading)
    from the SAME capture, so the scenario actually reaches a verdict rather
    than UNMEASURED or a cohort rejection."""
    entries = [
        {
            "kpi": "deck_load_to_stems_ready_s",
            "value": 26,
            "unit": "s",
            "date": "2026-09-09",
            "source": "manual capture",
            "capture_id": "s6-sess-1",
        },
        {
            "kpi": "deck_load_blocks_playback_ms",
            "value": 0,
            "unit": "ms blocked while stems load",
            "date": "2026-09-09",
            "source": "manual capture",
            "capture_id": "s6-sess-1",
        },
    ]
    assert _score_one("S6", entries).verdict == "OVER"


def test_s6_any_playback_blockage_is_breaking() -> None:
    """The reviewer's own reproduction: `deck_load_blocks_playback_ms` used to
    record `breaking: null`, so a 1ms blockage read OVER, never the BREAKING
    the spec's own breaking cell calls for ('blocks playability at all' - not
    a duration, any blockage). The KPI's own breaking bound is now 0, so any
    positive blockage must report BREAKING even with a fast, otherwise-
    passing load duration alongside it."""
    entries = [
        {
            "kpi": "deck_load_to_stems_ready_s",
            "value": 10,
            "unit": "s",
            "date": "2026-09-09",
            "source": "manual capture",
        },
        {
            "kpi": "deck_load_blocks_playback_ms",
            "value": 1,
            "unit": "ms blocked while stems load",
            "date": "2026-09-09",
            "source": "manual capture",
        },
    ]
    assert _score_one("S6", entries).verdict == "BREAKING"


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
