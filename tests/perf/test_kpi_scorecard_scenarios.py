"""Tests for each UX scenario's own SINGLE-required-KPI scoring, scored
against the REAL shipped docs/perf/kpi-map.json - distinct from the generic
verdict_for/newest_reading/age unit tests in test_kpi_scorecard.py, which use
synthetic fixture maps, and from the multi-KPI combination/cohort tests in
test_kpi_scorecard_cohort.py.

Split out of test_kpi_scorecard.py (Thu 10 Sep 2026) to keep that file under
the 600-line file-size ratchet, on the seam the original file already had:
generic band/reading-primitive tests above `_shipped_kpi_map`/`_score_one`,
per-scenario tests against the real map below it. The combination/cohort
tests were split out again the same day, for the same reason, once this
file's own growth crossed the ratchet a second time.
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


def test_s4_max_frame_gap_just_under_a_second_is_pass_not_breaking() -> None:
    """The boundary control for the fix above: 999ms is not yet 'greater
    than' the breaking bound (verdict_for's own strict `>` for a
    lower-is-better KPI), and max's zero-width band (budget = acceptable =
    breaking = 999) means anything at or below it is PASS outright - proving
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
            "value": 999,
            "unit": "ms max frame delta per 10s window",
            "date": "2026-09-09",
            "machine": "silver",
            "note": "n=10s capture window",
            "source": "manual capture",
            "capture_id": "s4-sess-1",
        },
    ]
    assert _score_one("S4", entries).verdict == "PASS"


def test_s4_exact_one_second_freeze_is_breaking_not_pass() -> None:
    """A third reviewer round's own reproduction: the first max-KPI attempt
    set budget = acceptable = breaking = 1000, so `verdict_for`'s
    `value <= budget` check caught an exact 1000ms gap - the literal
    one-second freeze this KPI exists to catch - and returned PASS before
    the breaking comparison ever ran. The band now sits at 999, one below
    the round number, so 1000ms itself lands past it and reports BREAKING."""
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
    assert _score_one("S4", entries).verdict == "BREAKING"


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


def test_s5_stays_unmeasured_without_a_cache_state_tag() -> None:
    """A reviewer's own reproduction: the shipped ledger's real
    packaged_deck_load_total_ms row names no warm/cold state, so scoring it
    against the blended budget/acceptable pair silently picked the generous
    cold band regardless of which state actually produced it. Any reading
    whose note is silent on cache state must go UNMEASURED, not guess."""
    entries = [
        {
            "kpi": "packaged_deck_load_total_ms",
            "value": 3796,
            "unit": "ms",
            "date": "2026-09-09",
            "machine": "air-packaged",
            "note": "max of 559-3796ms; decodeMix largest stage 8/8 (410-1188ms)",
            "source": "OpenDJ Diagnostics probe ring, 8 loads",
        }
    ]
    score = _score_one("S5", entries)
    assert score.verdict == UNMEASURED
    assert "cache state" in score.note


def test_s5_warm_reading_is_held_to_the_tight_warm_band() -> None:
    """The reviewer's exact failure mode: a WARM load that missed its own 2s
    target by nearly 4x must not borrow the cold population's generous 5s
    acceptable band just because both bands used to share one scale. Tagged
    warm, 2500ms clears budget's old value but must read OVER, not PASS or
    ACCEPTABLE."""
    entries = [
        {
            "kpi": "packaged_deck_load_total_ms",
            "value": 2500,
            "unit": "ms",
            "date": "2026-09-09",
            "machine": "air-packaged",
            "note": "warm cache, max of 8 loads",
            "source": "OpenDJ Diagnostics probe ring, 8 loads",
        }
    ]
    assert _score_one("S5", entries).verdict == "OVER"


def test_s5_cold_reading_gets_the_looser_cold_band() -> None:
    """The positive control for the fix above: the SAME 2500ms reading, when
    the note says cold instead of warm, is judged against the cold
    population's own 5s acceptable band and reads PASS - proving the gate
    changes the applied threshold, not just whether a state word is
    present."""
    entries = [
        {
            "kpi": "packaged_deck_load_total_ms",
            "value": 2500,
            "unit": "ms",
            "date": "2026-09-09",
            "machine": "air-packaged",
            "note": "cold cache, max of 8 loads",
            "source": "OpenDJ Diagnostics probe ring, 8 loads",
        }
    ]
    assert _score_one("S5", entries).verdict == "PASS"


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


