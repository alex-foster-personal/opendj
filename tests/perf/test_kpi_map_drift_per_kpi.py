"""Tests for `_per_kpi_drift`: a required entry's OWN numbers, direction, and
strictness against the spec cell it is scored against, distinct from the
scenario-headline checks in test_kpi_map_drift.py.

Split out of test_kpi_map_drift.py (Thu 10 Sep 2026) to keep that file under
the 600-line file-size ratchet, on the seam the module already has: everything
here is about a DICT-shaped required entry's own resolved values, while the
sibling file covers the scenario-level scalar checks.
"""

from __future__ import annotations

from scripts.perf.kpi_map_drift import threshold_drift


def test_per_kpi_drift_now_fails_closed_on_an_unverifiable_required_threshold() -> None:
    """The reviewer's own reproduction: S7's dropped-keystroke breaking bound
    is stated as prose ('or dropped keystrokes') with no literal digit in the
    cell, so a mutated breaking value (0 -> 999) had no strict-family
    candidate to compare against and was silently left unchecked before this
    fix - the exact stale-value drift this whole module exists to catch. An
    uncheckable required threshold must now fail closed (report a finding)
    rather than pass silently, unless the entry explicitly opts out via
    `unverifiable_columns` (see the control test right below)."""
    kpi_map = {
        "scenarios": {
            "S7": {
                "unit": "ms p95",
                "budget": 50,
                "acceptable": 100,
                "breaking": 250,
                "spec_cells": {
                    "target": "p95 <=50ms",
                    "acceptable": "<=100ms",
                    "breaking": ">250ms or dropped keystrokes",
                },
                "required": [
                    {
                        "kpi": "library_filter_keystroke_ms_p95",
                        "unit": "ms p95",
                        "budget": 50,
                        "acceptable": 100,
                        "breaking": 250,
                    },
                    {
                        "kpi": "library_filter_dropped_keystrokes_per_session",
                        "unit": "dropped keystrokes per session",
                        "budget": 0,
                        "acceptable": 0,
                        "breaking": 999,
                    },
                ],
            }
        }
    }
    findings = threshold_drift(kpi_map)
    assert any(
        "library_filter_dropped_keystrokes_per_session breaking" in f
        and "999" in f
        and "no comparable number" in f
        for f in findings
    )


def test_per_kpi_drift_stays_silent_when_a_column_opts_out_as_unverifiable() -> None:
    """The control for the fail-closed test above: the SAME uncheckable cell,
    but the entry now names 'breaking' in `unverifiable_columns` - an
    explicit, reviewed opt-out - so the same mutated 999 value must not fire.
    This is the deliberately-disclosed-limitation case (a prose-only cell
    with genuinely no digit), distinct from a value nobody reviewed."""
    kpi_map = {
        "scenarios": {
            "S7": {
                "unit": "ms p95",
                "budget": 50,
                "acceptable": 100,
                "breaking": 250,
                "spec_cells": {
                    "target": "p95 <=50ms",
                    "acceptable": "<=100ms",
                    "breaking": ">250ms or dropped keystrokes",
                },
                "required": [
                    {
                        "kpi": "library_filter_keystroke_ms_p95",
                        "unit": "ms p95",
                        "budget": 50,
                        "acceptable": 100,
                        "breaking": 250,
                    },
                    {
                        "kpi": "library_filter_dropped_keystrokes_per_session",
                        "unit": "dropped keystrokes per session",
                        "budget": 0,
                        "acceptable": 0,
                        "breaking": 999,
                        "unverifiable_columns": ["target", "acceptable", "breaking"],
                    },
                ],
            }
        }
    }
    assert threshold_drift(kpi_map) == []


def test_per_kpi_drift_skips_a_plain_string_required_entry() -> None:
    """A plain-string required entry inherits the scenario's own headline
    budget/acceptable/breaking verbatim (see resolve_required), so checking
    it here would only re-run the scalar check above a second time, and
    without that check's ms<->s conversion - S5's real shape, a `ms`-unit
    scenario scored against a `<=2s warm` cell, must not report uncheckable
    just because this stricter per-KPI matcher does not convert units."""
    kpi_map = {
        "scenarios": {
            "S5": {
                "unit": "ms",
                "budget": 2000,
                "acceptable": 5000,
                "breaking": 10000,
                "spec_cells": {
                    "target": "<=2s warm",
                    "acceptable": "<=5s cold",
                    "breaking": ">10s",
                },
                "required": ["packaged_deck_load_total_ms"],
            }
        }
    }
    assert threshold_drift(kpi_map) == []


def test_per_kpi_drift_fires_when_a_required_entrys_own_direction_disagrees() -> None:
    """The reviewer's own reproduction: S2's audible required entry gets its
    OWN `lower_is_better` flipped to False, sharing a cell with the visual
    entry whose direction is untouched. The scenario-level check alone
    cannot see this - the scenario headline `lower_is_better` is still True -
    so a 100ms reading would otherwise pass the whole scenario, undetected,
    exactly per the finding's own words."""
    kpi_map = {
        "scenarios": {
            "S2": {
                "unit": "ms",
                "lower_is_better": True,
                "budget": 30,
                "acceptable": 60,
                "breaking": 60,
                "spec_cells": {
                    "target": "<=30ms audible, <=16ms visual (LATENCY-01)",
                    "acceptable": "<=60ms",
                    "breaking": ">60ms (current e2e ceiling)",
                },
                "required": [
                    {
                        "kpi": "input_to_audible_ms_p99",
                        "unit": "ms",
                        "lower_is_better": False,
                        "budget": 30,
                        "acceptable": 60,
                        "breaking": 60,
                    },
                    {
                        "kpi": "visual_feedback_ms_p95",
                        "unit": "ms",
                        "lower_is_better": True,
                        "budget": 16,
                        "acceptable": 16,
                        "breaking": None,
                        "unverifiable_columns": ["acceptable"],
                    },
                ],
            }
        }
    }
    findings = threshold_drift(kpi_map)
    assert any(
        "input_to_audible_ms_p99 lower_is_better" in f and "False" in f for f in findings
    )
    assert not any("visual_feedback_ms_p95 lower_is_better" in f for f in findings)


def test_per_kpi_drift_fires_when_a_required_entrys_own_strictness_disagrees() -> None:
    """The reviewer's own reproduction: S4's dropped-frame entry loses its
    `acceptable_strict` flag while the spec cell still states the strict
    "<5%" it was added for, so a reading of exactly 5% would silently go
    back to reporting ACCEPTABLE."""
    kpi_map = {
        "scenarios": {
            "S4": {
                "unit": "ms p95 frame delta",
                "lower_is_better": True,
                "budget": 16.7,
                "acceptable": 33.3,
                "breaking": None,
                "spec_cells": {
                    "target": "p95 frame delta <= display refresh interval",
                    "acceptable": "<5% dropped frames per 10s window",
                    "breaking": "visible stutter / freeze while audible",
                },
                "required": [
                    {
                        "kpi": "waveform_frame_delta_ms_p95",
                        "unit": "ms p95 frame delta",
                        "lower_is_better": True,
                        "budget": 16.7,
                        "acceptable": 16.7,
                        "breaking": None,
                        "unverifiable_columns": ["target", "acceptable"],
                    },
                    {
                        "kpi": "waveform_dropped_frame_pct_10s",
                        "unit": "% dropped frames per 10s window",
                        "lower_is_better": True,
                        "budget": 0,
                        "acceptable": 5,
                        "breaking": None,
                        "unverifiable_columns": ["target"],
                    },
                ],
            }
        }
    }
    (finding,) = threshold_drift(kpi_map)
    assert "waveform_dropped_frame_pct_10s acceptable_strict" in finding and "False" in finding


def test_per_kpi_drift_stays_silent_on_direction_and_strictness_when_resolved() -> None:
    """The control for the two tests above: the shipped shape for both
    entries (own direction agreeing, `acceptable_strict` present and
    agreeing) must not fire."""
    kpi_map = {
        "scenarios": {
            "S2": {
                "unit": "ms",
                "lower_is_better": True,
                "budget": 30,
                "acceptable": 60,
                "breaking": 60,
                "spec_cells": {
                    "target": "<=30ms audible, <=16ms visual (LATENCY-01)",
                    "acceptable": "<=60ms",
                    "breaking": ">60ms (current e2e ceiling)",
                },
                "required": [
                    {
                        "kpi": "input_to_audible_ms_p99",
                        "unit": "ms",
                        "lower_is_better": True,
                        "budget": 30,
                        "acceptable": 60,
                        "breaking": 60,
                    },
                ],
            },
            "S4": {
                "unit": "ms p95 frame delta",
                "lower_is_better": True,
                "budget": 16.7,
                "acceptable": 33.3,
                "breaking": None,
                "spec_cells": {
                    "target": "p95 frame delta <= display refresh interval",
                    "acceptable": "<5% dropped frames per 10s window",
                    "breaking": "visible stutter / freeze while audible",
                },
                "required": [
                    {
                        "kpi": "waveform_dropped_frame_pct_10s",
                        "unit": "% dropped frames per 10s window",
                        "lower_is_better": True,
                        "budget": 0,
                        "acceptable": 5,
                        "acceptable_strict": True,
                        "breaking": None,
                        "unverifiable_columns": ["target"],
                    },
                ],
            },
        }
    }
    assert threshold_drift(kpi_map) == []
