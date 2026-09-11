"""Bar phase: every beat number is a 1-to-4 position, or the phase is unestablished.

THE DEFECT THESE TESTS PIN. Beat This!'s `infer_beat_numbers` counts upward
between consecutive downbeats without wrapping, so a bar whose downbeat the
model missed is numbered 1,2,3,4,5,6,7,8 rather than 1,2,3,4,1,2,3,4. Measured
on the committed round-1 artifact `ops/beatbench/round-1/raw-beatgrid_lane_t050.json`
(90 tracks): 24 tracks carried a number above 4 and one reached 20. A consumer
that reads `n` as a bar position, which is what rekordbox's PQTZ grid means by
it, would place bar-1 markers on beats that are not bar 1.

Every test below asserts the PRESENCE of a property (`1 <= n <= bar_beats`, a
named reason, a counted anomaly), never the absence of a symptom, and each
anomaly case is paired with a clean control that must NOT trip the same flag.
"""

from __future__ import annotations

import pytest

from apps.analysis_beatgrid.bar_phase import (
    BAR_BEATS,
    REASON_NO_BEATS,
    REASON_NO_DOWNBEAT_ANCHOR,
    assign_bar_phase,
)


def _grid(n_beats: int, period_s: float = 0.5, start_s: float = 0.0) -> list[float]:
    return [round(start_s + i * period_s, 6) for i in range(n_beats)]


# ----- The clean case, which is the control for everything below ----------


def test_a_complete_four_four_grid_numbers_one_to_four_and_is_established():
    beats = _grid(16)
    downbeats = [beats[i] for i in (0, 4, 8, 12)]

    phase = assign_bar_phase(beats, downbeats)

    assert phase.beat_numbers == [1, 2, 3, 4] * 4
    assert phase.bar_phase_unestablished is False
    assert phase.reason is None
    assert phase.n_bars_over_length == 0
    assert phase.n_bars_under_length == 0
    assert phase.max_bar_beats == BAR_BEATS
    assert phase.n_backprojected_beats == 0


def test_every_emitted_downbeat_receives_number_one():
    beats = _grid(16)
    downbeats = [beats[i] for i in (0, 4, 8, 12)]

    phase = assign_bar_phase(beats, downbeats)

    for time_s, number in zip(beats, phase.beat_numbers, strict=True):
        if time_s in downbeats:
            assert number == 1, f"downbeat at {time_s}s numbered {number}"


# ----- The reported defect ------------------------------------------------


def test_a_missed_downbeat_wraps_into_the_bar_cycle_instead_of_counting_past_four():
    """The 8-beat bar is numbered 1,2,3,4,1,2,3,4 and the anomaly is COUNTED.

    Wrapping alone would hide the missed downbeat, so the long bar has to
    survive into the result as a number a reader can act on.
    """
    beats = _grid(16)
    downbeats = [beats[i] for i in (0, 8, 12)]  # the downbeat at index 4 was missed

    phase = assign_bar_phase(beats, downbeats)

    assert phase.beat_numbers == [1, 2, 3, 4, 1, 2, 3, 4, 1, 2, 3, 4, 1, 2, 3, 4]
    assert max(phase.beat_numbers) <= BAR_BEATS
    assert phase.n_bars_over_length == 1
    assert phase.max_bar_beats == 8
    assert phase.bar_phase_unestablished is False


def test_no_beat_number_exceeds_the_bar_length_however_many_downbeats_are_missed():
    """Five consecutive missed downbeats, the shape that produced n=20 in round 1."""
    beats = _grid(40)
    downbeats = [beats[0], beats[20]]

    phase = assign_bar_phase(beats, downbeats)

    assert all(1 <= n <= BAR_BEATS for n in phase.beat_numbers)
    assert len(phase.beat_numbers) == len(beats)
    assert phase.max_bar_beats == 20
    # Two: the closed bar between the downbeats, and the final open bar, which
    # is 20 beats long and so is over-length for a reason the excerpt boundary
    # cannot explain away.
    assert phase.n_bars_over_length == 2


def test_a_short_bar_restarts_at_one_and_is_counted_separately_from_a_long_one():
    beats = _grid(11)
    downbeats = [beats[0], beats[3], beats[7]]  # bars of 3 and 4 beats

    phase = assign_bar_phase(beats, downbeats)

    assert phase.beat_numbers == [1, 2, 3, 1, 2, 3, 4, 1, 2, 3, 4]
    assert phase.n_bars_under_length == 1
    assert phase.n_bars_over_length == 0


# ----- Beats with no PRECEDING downbeat -----------------------------------


def test_a_pickup_run_is_back_projected_from_the_first_downbeat_and_counted():
    """Leading beats are anchored by the FOLLOWING downbeat, not left unnumbered.

    Measured on the committed round-1 artifact: 45 of the 86 tracks that have
    any downbeat at all start with beats before the first one. Treating those
    as unestablished would flag half the corpus for an ordinary pickup, so they
    are numbered by counting backwards from the anchor and reported as
    back-projected instead.
    """
    beats = _grid(11)
    downbeats = [beats[3], beats[7]]

    phase = assign_bar_phase(beats, downbeats)

    assert phase.beat_numbers[:3] == [2, 3, 4]
    assert phase.beat_numbers[3] == 1
    assert phase.n_backprojected_beats == 3
    assert phase.bar_phase_unestablished is False


def test_a_run_of_beats_with_no_downbeat_anywhere_is_unestablished_not_numbered():
    beats = _grid(16)

    phase = assign_bar_phase(beats, [])

    assert phase.bar_phase_unestablished is True
    assert phase.reason == REASON_NO_DOWNBEAT_ANCHOR
    assert phase.beat_numbers == []
    assert phase.n_beats == 16
    assert phase.n_downbeats == 0


def test_no_beats_at_all_is_unestablished_with_its_own_reason():
    phase = assign_bar_phase([], [])

    assert phase.bar_phase_unestablished is True
    assert phase.reason == REASON_NO_BEATS
    assert phase.beat_numbers == []


# ----- Fail-fast contracts ------------------------------------------------


def test_a_downbeat_that_is_not_a_beat_raises_rather_than_being_snapped():
    """Snapping here would hide an upstream bug in whatever produced the pair."""
    beats = _grid(8)

    with pytest.raises(ValueError, match="not a beat"):
        assign_bar_phase(beats, [0.25])


def test_unsorted_beat_times_raise_rather_than_producing_a_scrambled_phase():
    with pytest.raises(ValueError, match="ascending"):
        assign_bar_phase([0.0, 1.0, 0.5], [0.0])


def test_a_bar_length_below_one_is_refused():
    with pytest.raises(ValueError, match="bar_beats"):
        assign_bar_phase(_grid(4), [0.0], bar_beats=0)
