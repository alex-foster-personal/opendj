"""The own-fast bench quotes agreement over the frame denominator it claims (STRUCT-06).

[if] the bench pools tracks [then] agreement is over pooled frames, [else stop].

Acceptance lines:
  [if] two tracks of different lengths are scored [then] label agreement is
       agreeing frames over all both-labeled frames ⛔️ a per-track mean
       published as "share of time"
  [if] a track has no drop on either side [then] its frames still count in the
       pooled frame denominator ⛔️ a track silently leaving the denominator
"""

from __future__ import annotations

import pytest

from apps.analysis_structure.bench import (
    agreement_from_counts,
    boundary_prf,
    frame_counts,
    pooled_agreement,
)

pytestmark = pytest.mark.requirement("STRUCT-06")


def _row(ref: list[str], est: list[str]) -> dict:
    return {"counts": frame_counts(list(ref), list(est))}


def test_label_agreement_pools_frames_not_tracks():
    long = _row(["drop"] * 90 + ["intro"] * 10, ["drop"] * 90 + ["outro"] * 10)  # 90 of 100 agree
    short = _row(["intro"] * 10, ["outro"] * 10)  # 0 of 10 agree
    acc, _f1, frames = pooled_agreement([long, short], "counts")
    assert frames == 110
    assert acc == round(90 / 110, 3)
    # Control: the per-track mean is a different number, so the test can tell them apart.
    assert round((0.9 + 0.0) / 2, 3) != acc


def test_a_drop_free_track_has_no_f1_of_its_own_but_its_frames_still_count():
    drops = _row(["drop"] * 10, ["drop"] * 5 + ["intro"] * 5)  # tp 5, fn 5, 5 agree
    drop_free = _row(["intro"] * 10, ["intro"] * 10)  # no drop on either side, 10 agree
    assert agreement_from_counts(drop_free["counts"])[1] is None, "its own F1 is undefined"
    acc, f1, frames = pooled_agreement([drops, drop_free], "counts")
    assert frames == 20, "the drop-free track's frames are in the pooled denominator"
    assert acc == round(15 / 20, 3)
    # Control: true negatives never enter F1, so the drop-free track leaves it unchanged.
    assert f1 == round(2 * 5 / (2 * 5 + 5), 3)


def test_pooled_drop_f1_is_not_the_mean_of_per_track_f1():
    drops = _row(["drop"] * 10, ["drop"] * 5 + ["intro"] * 5)  # tp 5, fn 5: F1 0.667
    false_drops = _row(["intro"] * 10, ["drop"] * 10)  # fp 10: F1 0.0
    _acc, f1, _frames = pooled_agreement([drops, false_drops], "counts")
    assert f1 == round(2 * 5 / (2 * 5 + 10 + 5), 3)  # 0.4 from pooled tp/fp/fn
    # Control: the per-track mean would read 0.333.
    assert f1 != round((2 * 5 / (2 * 5 + 5) + 0.0) / 2, 3)


def test_frames_where_one_side_did_not_label_are_outside_the_denominator():
    row = {"counts": frame_counts(["drop", None, "intro"], ["drop", "drop", None])}
    assert row["counts"]["frames"] == 1


def test_boundary_matching_finds_the_maximum_number_of_hits():
    """Nearest-pair-first would spend 1.1<->0.9 and report one hit; two pairs exist."""
    p, r, f = boundary_prf([0.0, 1.1], [0.9, 2.0], tol=1.0)
    assert (p, r, f) == (1.0, 1.0, 1.0)
    # Control: a boundary outside every tolerance window still misses.
    assert boundary_prf([0.0, 5.0], [0.9, 2.0], tol=1.0)[0] == 0.5
