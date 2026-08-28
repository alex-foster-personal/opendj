"""Phase 4 SYNC-04 Plan 3: cue-array resolver tests."""
from __future__ import annotations

import pytest

from apps.shared.normalised import NormalisedCue
from apps.sync.conflict import resolve_cue_array

pytestmark = pytest.mark.requirement("SYNC-04")


def test_resolve_cue_array_identical_inputs_no_diff():
    cues = [NormalisedCue(position_msec=p, kind="memory") for p in (0, 1000, 2000)]
    res = resolve_cue_array(cues, cues)
    assert res.rb_additions == () and res.djay_additions == ()
    assert res.conflicts == ()


def test_rb_additions_when_djay_has_unique():
    rb = [NormalisedCue(position_msec=1000, kind="memory")]
    dj = [
        NormalisedCue(position_msec=1000, kind="memory"),
        NormalisedCue(position_msec=5000, kind="memory"),
    ]
    res = resolve_cue_array(rb, dj)
    assert len(res.rb_additions) == 1
    assert res.rb_additions[0].position_msec == 5000


def test_djay_additions_when_rb_has_unique():
    rb = [
        NormalisedCue(position_msec=1000, kind="memory"),
        NormalisedCue(position_msec=5000, kind="memory"),
    ]
    dj = [NormalisedCue(position_msec=1000, kind="memory")]
    res = resolve_cue_array(rb, dj)
    assert len(res.djay_additions) == 1
    assert res.djay_additions[0].position_msec == 5000


def test_tolerance_within_20ms_matches():
    rb = [NormalisedCue(position_msec=1000, kind="memory")]
    dj = [NormalisedCue(position_msec=1018, kind="memory")]
    res = resolve_cue_array(rb, dj, tolerance_msec=20)
    assert not res.rb_additions and not res.djay_additions


def test_tolerance_outside_does_not_match():
    rb = [NormalisedCue(position_msec=1000, kind="memory")]
    dj = [NormalisedCue(position_msec=1050, kind="memory")]
    res = resolve_cue_array(rb, dj, tolerance_msec=20)
    assert len(res.rb_additions) == 1 and len(res.djay_additions) == 1


def test_name_conflict_on_matched_pair():
    rb = [NormalisedCue(position_msec=1000, kind="memory", name="A")]
    dj = [NormalisedCue(position_msec=1000, kind="memory", name="B")]
    res = resolve_cue_array(rb, dj)
    assert len(res.conflicts) == 1


def test_color_conflict_on_matched_hot_cue():
    rb = [NormalisedCue(position_msec=1000, kind="hot", index=0, color_rgb=(255, 0, 0))]
    dj = [NormalisedCue(position_msec=1000, kind="hot", index=0, color_rgb=(0, 255, 0))]
    res = resolve_cue_array(rb, dj)
    assert len(res.conflicts) == 1


def test_hot_cue_index_collision_far_positions_is_conflict():
    rb = [NormalisedCue(position_msec=1000, kind="hot", index=3)]
    dj = [NormalisedCue(position_msec=20000, kind="hot", index=3)]
    res = resolve_cue_array(rb, dj)
    assert len(res.conflicts) == 1


def test_loop_length_mismatch_is_conflict():
    rb = [NormalisedCue(position_msec=1000, kind="loop", loop_length_msec=2000)]
    dj = [NormalisedCue(position_msec=1000, kind="loop", loop_length_msec=4000)]
    res = resolve_cue_array(rb, dj)
    assert len(res.conflicts) == 1


def test_empty_both_sides():
    res = resolve_cue_array([], [])
    assert res.rb_additions == () and res.djay_additions == () and res.conflicts == ()


def test_tolerance_boundary_exact_20ms():
    rb = [NormalisedCue(position_msec=1000, kind="memory")]
    dj = [NormalisedCue(position_msec=1020, kind="memory")]
    res = resolve_cue_array(rb, dj, tolerance_msec=20)
    assert not res.rb_additions and not res.djay_additions
