"""H6: a targeted throughput re-measure must not destroy another tier's data.

`scripts/bench/tier_throughput.py` is the ONLY sanctioned writer of
`tier_throughput.json`, which is the only thing `apps/stems/tiers.py` estimates
cost from. Three invariants earned the hard way, none of them previously
imported by a test:

  * MERGE, never replace, keyed by (tier_key, gpu). `--tier L` once wiped S and
    M from the file including their raw points.
  * A fit refuses fewer than three distinct durations. A line through two
    points cannot separate the fixed term from the slope.
  * A fit below r^2 0.90 is still WRITTEN, carrying its note. The first run of
    this benchmark produced a physically impossible negative slope at r^2 0.27
    and only the note made it visible.

Acceptance criteria (UNCAPTURED-REQUIREMENTS.md H6):
  [if] M@H100 is measured, then L@H100 separately [then] the file still
       contains M@H100 and its raw points ⛔️ a targeted refinement deletes the
       comparison it was refining
  [if] only two distinct durations are available [then] the fit refuses, naming
       the count ⛔️ a line through two points ships as a measurement
  [if] a fit lands below r^2 0.90 [then] the row is written carrying the
       poor-fit note ⛔️ a bad fit is laundered into a confident cost estimate

-Claude
"""
from __future__ import annotations

from typing import Any

import pytest

from scripts.bench import tier_throughput as tt


def _rows(tier: str, points: list[tuple[float, float, float]]) -> list[dict[str, Any]]:
    """Raw rows in the shape `_measure_tier` emits."""
    return [
        {
            "tier": tier,
            "stable_id": f"{tier}-{i}",
            "rep": 0,
            "duration_s": dur,
            "audio_minutes": round(dur / 60.0, 4),
            "wall_s": wall,
            "container_s": container,
            "timings": {},
        }
        for i, (dur, wall, container) in enumerate(points)
    ]


# ----- MERGE, never replace ---------------------------------------------


@pytest.mark.requirement("STEM-TIERS")
def test_remeasuring_one_tier_keeps_the_other_tiers_and_their_raw_points() -> None:
    prior_m = tt._fit("M", "H100", _rows("M", [(150.0, 30.0, 20.0),
                                               (330.0, 55.0, 40.0),
                                               (600.0, 95.0, 70.0)]), "cmd-M")
    prior = tt.merge_blob({}, [prior_m], _rows("M", [(150.0, 30.0, 20.0),
                                                     (330.0, 55.0, 40.0),
                                                     (600.0, 95.0, 70.0)]), "H100")
    assert [m["tier_key"] for m in prior["measurements"]] == ["M"]

    l_raw = _rows("L", [(150.0, 60.0, 45.0), (330.0, 110.0, 90.0),
                        (600.0, 190.0, 160.0)])
    after = tt.merge_blob(prior, [tt._fit("L", "H100", l_raw, "cmd-L")], l_raw, "H100")

    keys = {(m["tier_key"], m["gpu"]) for m in after["measurements"]}
    assert ("M", "H100") in keys, "a targeted --tier L re-measure deleted M@H100"
    assert ("L", "H100") in keys
    kept_m_raw = [r for r in after["raw"] if r["tier"] == "M"]
    assert len(kept_m_raw) == 3, (
        "M's raw points were dropped; the refit that needs them is now impossible"
    )


@pytest.mark.requirement("STEM-TIERS")
def test_remeasuring_the_same_pair_replaces_only_that_pairs_rows() -> None:
    first = _rows("M", [(150.0, 30.0, 20.0), (330.0, 55.0, 40.0),
                        (600.0, 95.0, 70.0)])
    prior = tt.merge_blob({}, [tt._fit("M", "H100", first, "first")], first, "H100")
    second = _rows("M", [(150.0, 31.0, 21.0), (330.0, 56.0, 41.0),
                         (600.0, 96.0, 71.0)])
    after = tt.merge_blob(
        prior, [tt._fit("M", "H100", second, "second")], second, "H100"
    )
    assert len(after["measurements"]) == 1
    assert after["measurements"][0]["measured_by"] == "second"
    assert len(after["raw"]) == 3, "stale raw points for the re-measured pair survived"
    assert {r["wall_s"] for r in after["raw"]} == {31.0, 56.0, 96.0}


@pytest.mark.requirement("STEM-TIERS")
def test_the_same_tier_on_a_different_card_is_a_different_row() -> None:
    a_raw = _rows("M", [(150.0, 30.0, 20.0), (330.0, 55.0, 40.0),
                        (600.0, 95.0, 70.0)])
    prior = tt.merge_blob({}, [tt._fit("M", "A10G", a_raw, "a")], a_raw, "A10G")
    h_raw = _rows("M", [(150.0, 20.0, 14.0), (330.0, 36.0, 27.0),
                        (600.0, 62.0, 48.0)])
    after = tt.merge_blob(prior, [tt._fit("M", "H100", h_raw, "h")], h_raw, "H100")
    assert {(m["tier_key"], m["gpu"]) for m in after["measurements"]} == {
        ("M", "A10G"),
        ("M", "H100"),
    }


# ----- the fit refuses too few points -----------------------------------


@pytest.mark.requirement("STEM-TIERS")
def test_fit_refuses_two_distinct_durations_and_names_the_count() -> None:
    two = _rows("S", [(150.0, 20.0, 14.0), (600.0, 60.0, 48.0)])
    with pytest.raises(SystemExit) as exc:
        tt._fit("S", "H100", two, "cmd")
    msg = str(exc.value)
    assert "2 distinct duration" in msg, f"refusal must name the count, got: {msg}"
    assert str(tt.MIN_DISTINCT_DURATIONS) in msg


@pytest.mark.requirement("STEM-TIERS")
def test_repeats_of_the_same_duration_do_not_count_as_distinct() -> None:
    """Three rows, two durations. Repeats raise confidence, not rank."""
    rows = _rows("S", [(150.0, 20.0, 14.0), (150.0, 21.0, 15.0),
                       (600.0, 60.0, 48.0)])
    with pytest.raises(SystemExit) as exc:
        tt._fit("S", "H100", rows, "cmd")
    assert "2 distinct duration" in str(exc.value)


# ----- a poor fit ships WITH its note ------------------------------------


@pytest.mark.requirement("STEM-TIERS")
def test_poor_fit_is_written_carrying_the_note_not_dropped() -> None:
    """Wildly non-linear wall times: the row must still exist, noted."""
    noisy = _rows("S", [(150.0, 90.0, 14.0), (330.0, 10.0, 27.0),
                        (600.0, 80.0, 48.0)])
    fit = tt._fit("S", "H100", noisy, "cmd")
    assert fit["r_squared"] < tt.MIN_R_SQUARED
    assert "POOR FIT" in fit["note"], (
        "a below-threshold fit was laundered into a confident estimate"
    )
    assert str(tt.MIN_R_SQUARED) in fit["note"]


@pytest.mark.requirement("STEM-TIERS")
def test_clean_fit_carries_no_note() -> None:
    clean = _rows("S", [(150.0, 30.0, 20.0), (330.0, 54.0, 40.0),
                        (600.0, 94.0, 70.0)])
    fit = tt._fit("S", "H100", clean, "cmd")
    assert fit["r_squared"] >= tt.MIN_R_SQUARED
    assert fit["note"] == ""


@pytest.mark.requirement("STEM-TIERS")
def test_every_emitted_row_names_the_command_that_produced_it() -> None:
    clean = _rows("S", [(150.0, 30.0, 20.0), (330.0, 54.0, 40.0),
                        (600.0, 94.0, 70.0)])
    fit = tt._fit("S", "H100", clean, "uv run ... --tier S")
    assert fit["measured_by"] == "uv run ... --tier S"
    assert fit["duration_span_s"] == [150.0, 330.0, 600.0]
