"""Phase 4 SYNC-04 read-side: cue comparison audit tests."""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from apps.audit.cue_comparison import (
    CueDiffRow,
    compare_cue_lists,
    write_diff_csv,
)
from apps.shared.normalised import NormalisedCue

pytestmark = pytest.mark.requirement("SYNC-04")


def test_identical_lists_have_no_diffs():
    cues = [NormalisedCue(position_msec=p, kind="memory") for p in (0, 1000, 2000)]
    rb_only, djay_only, conflicts = compare_cue_lists(cues, cues)
    assert rb_only == []
    assert djay_only == []
    assert conflicts == []


def test_rb_only_positions_reported():
    rb = [NormalisedCue(position_msec=500, kind="memory")]
    djay = [NormalisedCue(position_msec=2000, kind="memory")]
    rb_only, djay_only, conflicts = compare_cue_lists(rb, djay)
    assert rb_only == [500]
    assert djay_only == [2000]
    assert conflicts == []


def test_tolerance_matches_within_20ms():
    rb = [NormalisedCue(position_msec=1000, kind="memory")]
    djay = [NormalisedCue(position_msec=1018, kind="memory")]
    rb_only, djay_only, conflicts = compare_cue_lists(rb, djay)
    assert rb_only == [] and djay_only == [] and conflicts == []


def test_tolerance_boundary_21ms_is_not_match():
    rb = [NormalisedCue(position_msec=1000, kind="memory")]
    djay = [NormalisedCue(position_msec=1021, kind="memory")]
    rb_only, djay_only, _ = compare_cue_lists(rb, djay)
    assert rb_only == [1000] and djay_only == [1021]


def test_name_mismatch_is_conflict():
    rb = [NormalisedCue(position_msec=1000, kind="memory", name="A")]
    djay = [NormalisedCue(position_msec=1000, kind="memory", name="B")]
    rb_only, djay_only, conflicts = compare_cue_lists(rb, djay)
    assert conflicts == [1000]
    assert rb_only == [] and djay_only == []


def test_color_mismatch_is_conflict():
    rb = [NormalisedCue(position_msec=1000, kind="hot", index=0, color_rgb=(255, 0, 0))]
    djay = [NormalisedCue(position_msec=1000, kind="hot", index=0, color_rgb=(0, 255, 0))]
    _, _, conflicts = compare_cue_lists(rb, djay)
    assert conflicts == [1000]


def test_kind_mismatch_is_not_paired():
    rb = [NormalisedCue(position_msec=1000, kind="memory")]
    djay = [NormalisedCue(position_msec=1000, kind="hot", index=0)]
    rb_only, djay_only, conflicts = compare_cue_lists(rb, djay)
    assert rb_only == [1000] and djay_only == [1000] and conflicts == []


def test_hot_index_mismatch_is_not_paired():
    rb = [NormalisedCue(position_msec=1000, kind="hot", index=0)]
    djay = [NormalisedCue(position_msec=1000, kind="hot", index=3)]
    rb_only, djay_only, _ = compare_cue_lists(rb, djay)
    assert rb_only == [1000] and djay_only == [1000]


def test_loop_length_mismatch_is_conflict():
    rb = [
        NormalisedCue(
            position_msec=1000,
            kind="loop",
            loop_length_msec=2000,
        )
    ]
    djay = [
        NormalisedCue(
            position_msec=1000,
            kind="loop",
            loop_length_msec=4000,
        )
    ]
    _, _, conflicts = compare_cue_lists(rb, djay)
    assert conflicts == [1000]


def test_write_diff_csv_round_trip(tmp_path: Path):
    rows = [
        CueDiffRow(
            rb_content_id="1",
            djay_uuid="abc",
            rb_cue_count=2,
            djay_cue_count=1,
            rb_only_positions=(500,),
            djay_only_positions=(),
            conflicting_positions=(1000,),
            union_count=3,
        )
    ]
    out = tmp_path / "cue-diff.csv"
    write_diff_csv(rows, out)
    assert out.exists()
    with out.open() as fp:
        reader = csv.DictReader(fp)
        records = list(reader)
    assert len(records) == 1
    rec = records[0]
    assert rec["rb_content_id"] == "1"
    assert rec["rb_only_positions"] == "500"
    assert rec["conflicting_positions"] == "1000"


def test_empty_sides_both_handled():
    rb_only, djay_only, conflicts = compare_cue_lists([], [])
    assert rb_only == [] and djay_only == [] and conflicts == []
