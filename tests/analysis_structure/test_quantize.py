"""Bar quantizer: boundaries land on own downbeats, phrases follow the music, none move silently."""

from __future__ import annotations

import pytest

from apps.analysis_structure.quantize import quantize, vote_phrase_offset

# 120 BPM in 4/4: a bar is 2.0 s, downbeats at 0, 2, 4, ... 398 (200 bars).
GRID = [2.0 * i for i in range(200)]


def seg(*starts: float, label: str = "verse") -> list[tuple[float, float, str]]:
    ends = [*starts[1:], 400.0]
    return [(s, e, label) for s, e in zip(starts, ends, strict=True)]


def test_boundary_snaps_to_nearest_downbeat_and_records_distance():
    q = quantize(seg(0.0, 16.3), GRID, phrase_bars=1)
    [b] = q.boundaries
    assert b.time_s == 16.0 and b.bar == 8
    assert b.raw_s == 16.3 and b.moved_s == pytest.approx(0.3)
    assert b.status == "snapped"


def test_boundary_more_than_a_bar_from_any_downbeat_is_left_and_flagged():
    # A grid with a 10 s gap: a boundary in the middle of it is 5 s from any downbeat.
    grid = [0.0, 2.0, 4.0, 14.0, 16.0, 18.0]
    q = quantize(seg(0.0, 9.0), grid, phrase_bars=1)
    [b] = q.boundaries
    assert b.status == "unsnapped" and b.time_s == 9.0 and b.bar is None
    assert "boundary_moved_over_a_bar" in q.implausible


def test_phrase_snap_moves_at_most_one_bar():
    # bar 17 -> phrase bar 16 (one bar); bar 18 is two bars from 16 and 20, so it stays.
    q = quantize(seg(0.0, 34.1, 100.0), GRID, phrase_bars=4, phrase_offset=0)
    first, second = q.boundaries
    assert first.bar == 16 and first.phrase_aligned
    assert (
        second.bar == 50 and not second.phrase_aligned
    )  # 50 % 4 == 2: a tie, left on its downbeat


def test_phrase_offset_is_voted_from_the_music_not_bar_one():
    # A 2-bar pickup: every real phrase starts on bar 2 mod 4.
    starts = [0.0] + [2.0 * b + 0.1 for b in (2, 10, 18, 34, 50)]
    q = quantize(seg(*starts), GRID, phrase_bars=4)
    assert q.phrase_offset == 2
    assert [b.bar for b in q.boundaries] == [2, 10, 18, 34, 50]
    assert all(b.phrase_aligned for b in q.boundaries)


def test_pinned_offset_zero_counts_from_the_first_downbeat():
    starts = [0.0, 2.0 * 10 + 0.1]
    q = quantize(seg(*starts), GRID, phrase_bars=4, phrase_offset=0)
    assert q.phrase_offset == 0
    assert q.boundaries[0].bar == 10 and not q.boundaries[0].phrase_aligned


def test_two_boundaries_on_one_bar_merge_and_are_counted():
    q = quantize(seg(0.0, 32.2, 32.6), GRID, phrase_bars=1)
    assert [b.bar for b in q.boundaries] == [16]
    assert q.merged == 1


def test_edge_labels_and_time_zero_are_not_structure():
    segs = [
        (0.0, 0.5, "start"),
        (0.5, 64.0, "intro"),
        (64.0, 390.0, "chorus"),
        (390.0, 400.0, "end"),
    ]
    q = quantize(segs, GRID, phrase_bars=1)
    assert [b.label for b in q.boundaries] == ["intro", "chorus"]


def test_no_grid_keeps_model_times_and_says_so():
    q = quantize(seg(0.0, 30.0), [], phrase_bars=4)
    assert [b.time_s for b in q.boundaries] == [30.0]
    assert q.implausible == ["no_downbeat_grid"]


def test_short_section_and_sparse_track_are_flagged_not_hidden():
    q = quantize(seg(0.0, 32.0, 36.0), GRID, phrase_bars=1)  # a 2-bar section
    assert "section_under_4_bars" in q.implausible
    sparse = quantize(seg(0.0, 100.0), GRID, phrase_bars=1)  # 1 boundary over ~400 s
    assert "too_few_boundaries" in sparse.implausible


def test_plausible_track_raises_no_flags():
    starts = [0.0] + [2.0 * b for b in range(16, 200, 16)]
    q = quantize(seg(*starts), GRID, phrase_bars=4)
    assert q.implausible == []


def test_sections_are_pssi_shaped_with_bar_counts():
    q = quantize(seg(0.0, 32.0, 96.0), GRID, phrase_bars=4)
    s = q.sections(400.0)
    assert s[0] == {"start_s": 32.0, "end_s": 96.0, "label": "verse", "start_bar": 16, "bars": 32}
    assert s[-1]["end_s"] == 400.0 and s[-1]["bars"] is None


def test_vote_ties_go_to_zero():
    assert vote_phrase_offset([0, 1], 4) == 0
    assert vote_phrase_offset([], 4) == 0


def test_phrase_bars_must_be_positive():
    with pytest.raises(ValueError):
        quantize(seg(0.0, 10.0), GRID, phrase_bars=0)
