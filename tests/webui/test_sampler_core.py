"""Edge sampler pure slot core: grid snap, peaks, Camelot distance, ranking and slot ids.

[if] the slot core snaps, peaks, ranks or ids a slot wrongly [then] these tests fail, [else stop].

Regression one-liners:
  - if a loop cue's in_ms is not snapped to the nearest existing grid beat then broken
  - if a grid with under 2 beats or a non-increasing beat counts as usable then broken
  - if slot_id is not "<stable_id>:<source>:<in_ms>-<out_ms>" or does not parse back then broken
  - if peaks are not 120 points per band within 0..1 then broken
  - if compat ranks an out-of-window slot above an in-window one then broken
  - if the same slots in another order rank differently then broken
"""

from __future__ import annotations

import random
from typing import Any

import pytest

from apps.webui.server import sampler_core as core

GRID = [i * 500.0 for i in range(64)]  # 120 BPM, a beat every 500 ms
SID = "a" * 40


def _slot(letter: str | None, bpm: float, key: str | None, title: str = "t", **extra: Any) -> dict[str, Any]:
    return {
        "slot_id": f"{SID}:loop_hot_cue:{letter}",
        "stable_id": SID,
        "cue_letter": letter,
        "in_ms": 0,
        "native_bpm": bpm,
        "key": key,
        "title": title,
        "artist": "a",
        "name": None,
        **extra,
    }


def _letters(slots: list[Any]) -> list[str | None]:
    return [s["cue_letter"] for s in slots]


# ---------- grid snap ----------------------------------------------------------------


def test_if_a_loop_cue_starts_30_ms_late_then_it_snaps_to_the_beat_and_out_is_beats_later() -> None:
    """[if] a loop starts 30 ms after a beat [then] in is that beat and out 16 beats on, [else stop]."""
    r = core.snap_region(GRID, 4030.0, 12010.0)
    assert r == core.Region(in_ms=4000, out_ms=12000, beats=16, native_bpm=120.0)


def test_if_a_capture_gives_beats_then_out_is_that_many_grid_beats_after_in() -> None:
    """[if] a capture names 4 beats [then] out is the 4th beat after the snapped in, [else stop]."""
    assert core.snap_region(GRID, 1240.0, 99999.0, beats=4) == core.Region(1000, 3000, 4, 120.0)


def test_if_the_grid_varies_tempo_then_the_region_uses_its_own_beat_times() -> None:
    """[if] beats widen mid-track [then] out and native_bpm come from those beat times, [else stop]."""
    grid = [0.0, 500.0, 1000.0, 1600.0, 2200.0]
    assert core.snap_region(grid, 0.0, 0.0, beats=4) == core.Region(0, 2200, 4, 109.091)


@pytest.mark.parametrize(("in_ms", "out_ms", "beats"), [(4000.0, 4100.0, None), (30000.0, 0.0, 8), (0.0, 0.0, 0)])
def test_if_a_region_is_under_one_beat_or_past_the_grid_then_no_region(
    in_ms: float, out_ms: float, beats: int | None
) -> None:
    """[if] a region is shorter than a beat or runs past the last beat [then] it is None, [else stop]."""
    assert core.snap_region(GRID, in_ms, out_ms, beats) is None


@pytest.mark.parametrize(("beats", "usable"), [([], False), ([0.0], False), ([0.0, 0.0], False), ([0.0, 500.0], True)])
def test_if_a_grid_has_under_two_rising_beats_then_it_is_not_usable(beats: list[float], usable: bool) -> None:
    """[if] a grid has under 2 beats or a beat that does not rise [then] it is not usable, [else stop]."""
    assert core.usable_grid(beats) is usable


# ---------- peaks --------------------------------------------------------------------


def _analysis(values: list[float]) -> core.TrackAnalysis:
    return core.TrackAnalysis(beats_ms=GRID, bands={"low": values, "mid": values, "high": values}, duration_ms=32000.0)


def test_if_a_slot_is_listed_then_peaks_are_120_points_within_0_1() -> None:
    """[if] peaks are cut for a region [then] each band has 120 values within 0..1, [else stop]."""
    peaks = core.region_peaks(_analysis([(i % 7) / 7 for i in range(3200)]), 4000, 12000)
    assert all(len(peaks[b]) == 120 and all(0.0 <= v <= 1.0 for v in peaks[b]) for b in ("low", "mid", "high"))


def test_if_peaks_are_cut_then_they_cover_exactly_the_region() -> None:
    """[if] only the region is loud [then] every one of its 120 peaks is 1.0, [else stop]."""
    values = [1.0 if 400 <= i < 1200 else 0.0 for i in range(3200)]  # 100 points per second
    assert core.region_peaks(_analysis(values), 4000, 12000)["low"] == [1.0] * 120


@pytest.mark.parametrize("values", [[], [0.5, 1.5]])
def test_if_a_band_is_empty_or_out_of_range_then_peaks_raise(values: list[float]) -> None:
    """[if] a waveform band is empty or holds a value above 1 [then] ValueError, [else stop]."""
    with pytest.raises(ValueError):
        core.region_peaks(_analysis(values), 0, 32000)


# ---------- slot ids -----------------------------------------------------------------


def test_if_a_slot_is_listed_then_its_id_is_stable_id_source_region() -> None:
    """[if] a slot id is built [then] it reads stable_id:source:in-out, [else stop]."""
    assert (
        core.slot_id_of(SID, core.SOURCE_LOOP, core.Region(4000, 12000, 16, 120.0)) == f"{SID}:loop_hot_cue:4000-12000"
    )


def test_if_a_stable_id_holds_a_colon_then_the_slot_id_still_parses_back() -> None:
    """[if] a stable id holds ':' [then] parse_slot_id returns its parts unchanged, [else stop]."""
    slot_id = core.slot_id_of("spotify-pending:x", core.SOURCE_CAPTURE, core.Region(1, 2, 1, 1.0))
    assert core.parse_slot_id(slot_id) == core.SlotId("spotify-pending:x", "capture", 1, 2)


@pytest.mark.parametrize("bad", ["no-colons", f"{SID}:capture:1-x", f"{SID}:capture:12"])
def test_if_a_slot_id_is_malformed_then_it_parses_to_none(bad: str) -> None:
    """[if] a slot id lacks its parts or a numeric region [then] parse_slot_id is None, [else stop]."""
    assert core.parse_slot_id(bad) is None


# ---------- key and ranking ----------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b", "dist"),
    [("8A", "8A", 0), ("8A", "9A", 1), ("12A", "1A", 1), ("8A", "8B", 2), ("8A", "10A", 5), ("Am", "8A", 0)],
)
def test_if_two_keys_are_compared_then_camelot_distance_orders_same_near_relative_far(
    a: str, b: str, dist: int
) -> None:
    """[if] two keys are compared [then] same is 0, +-1 is 1, relative 2, else 3 + steps, [else stop]."""
    assert core.camelot_distance(a, b) == dist


@pytest.mark.parametrize("unknown", [None, "", "13A", "zz"])
def test_if_a_key_is_unknown_then_its_distance_ranks_last(unknown: str | None) -> None:
    """[if] a key is missing or unreadable [then] its distance is UNKNOWN_KEY_DISTANCE, [else stop]."""
    assert core.camelot_distance("8A", unknown) == core.UNKNOWN_KEY_DISTANCE


def test_if_master_is_given_then_compat_ranks_in_window_bpm_first() -> None:
    """[if] a master plays at 124 BPM in 8A [then] in-window slots rank before out-of-window, [else stop]."""
    slots = [_slot("A", 100.0, "8A"), _slot("B", 140.0, "8A"), _slot("C", 124.0, "3B"), _slot("D", 126.0, "8A")]
    assert _letters(core.rank(slots, "compat", 124.0, "8A", {}, {})) == ["D", "B", "C", "A"]


def test_if_no_master_plays_then_compat_groups_by_track_title() -> None:
    """[if] compat is asked with no master BPM [then] slots come in title order, [else stop]."""
    slots = [_slot("A", 120.0, "8A", title="Zed"), _slot("B", 120.0, "8A", title="alpha")]
    assert _letters(core.rank(slots, "compat", None, None, {}, {})) == ["B", "A"]


def test_if_sort_is_recent_then_the_newest_stamp_comes_first_and_unstamped_last() -> None:
    """[if] sort is recent [then] newest stamp first, unstamped after in title order, [else stop]."""
    slots = [_slot(x, 120.0, "8A", title=x) for x in "ABCD"]
    recency = {f"{SID}:loop_hot_cue:B": "2026-11-01", f"{SID}:loop_hot_cue:D": "2026-11-12"}
    assert _letters(core.rank(slots, "recent", None, None, recency, {})) == ["D", "B", "A", "C"]


@pytest.mark.parametrize("sort", ["compat", "track", "recent", "crate", "color"])
def test_if_the_same_slots_arrive_shuffled_then_the_ranking_is_identical(sort: core.SlotSort) -> None:
    """[if] the same slots arrive in another order [then] every sort ranks them the same, [else stop]."""
    slots = [
        _slot(chr(65 + i % 8), 110.0 + i % 30, f"{1 + i % 12}A", title=f"t{i % 5}", in_ms=i, color_index=i % 4 or None)
        for i in range(60)
    ]
    shuffled = random.Random(7).sample(slots, len(slots))
    crate_of = {SID: 0}
    assert core.rank(shuffled, sort, 124.0, "8A", {}, crate_of) == core.rank(slots, sort, 124.0, "8A", {}, crate_of)


def test_if_sort_is_unknown_then_rank_raises() -> None:
    """[if] rank is given a sort it does not know [then] it raises ValueError, [else stop]."""
    with pytest.raises(ValueError, match="unknown sampler sort"):
        core.rank([], "nope", None, None, {}, {})  # type: ignore[arg-type]


def test_if_a_track_is_in_two_crates_then_its_position_is_the_first_crate_in_library_order() -> None:
    """[if] a track sits in crates 0 and 2 [then] crate_positions gives it 0, others their own, [else stop]."""
    assert core.crate_positions([("t1",), ("t2",), ("t1", "t3")]) == {"t1": 0, "t2": 1, "t3": 2}


def test_if_q_matches_name_title_artist_or_key_then_the_slot_matches() -> None:
    """[if] q appears in any case in title or key [then] it matches, and a miss does not, [else stop]."""
    slot = _slot("A", 120.0, "8A", title="Deep House")
    assert (core.matches_query(slot, "deep"), core.matches_query(slot, "8a"), core.matches_query(slot, "zzz")) == (
        True,
        True,
        False,
    )
