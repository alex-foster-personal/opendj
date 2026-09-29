"""Unit tests for lock_bar_phase: every-downbeat majority vote and deck-legal cadence."""

from __future__ import annotations

import pytest

from apps.analysis_beatgrid.bar_phase import (
    BAR_BEATS,
    BAR_PHASE_AGREEMENT_FLOOR,
    REASON_BAR_PHASE_BELOW_FLOOR,
    REASON_NO_BEATS,
    REASON_NO_DOWNBEAT_ANCHOR,
    lock_bar_phase,
    vote_bar_phase,
)


def _grid(n_beats: int, period_s: float = 0.5, start_s: float = 0.0) -> list[float]:
    return [round(start_s + i * period_s, 6) for i in range(n_beats)]


def test_perfect_four_four_grid_locks_phase_zero_with_full_agreement() -> None:
    beats = _grid(16)
    downbeats = [beats[i] for i in (0, 4, 8, 12)]

    phase = lock_bar_phase(beats, downbeats)

    assert phase.bar_phase_unestablished is False
    assert phase.beat_numbers == [1, 2, 3, 4] * 4
    assert phase.chosen_phase == 0
    assert phase.phase_agreement == 1.0
    assert phase.n_downbeats_thinned == 0
    assert phase.n_phase_disagreements == 0


def test_detector_doubles_are_thinned_and_do_not_receive_bar_one() -> None:
    beats = _grid(20)
    downbeats = [beats[i] for i in (0, 1, 2, 4, 8, 12, 16)]

    phase = lock_bar_phase(beats, downbeats)

    assert phase.bar_phase_unestablished is False
    assert phase.phase_agreement == 1.0
    assert phase.n_downbeats_thinned > 0
    assert all(1 <= n <= BAR_BEATS for n in phase.beat_numbers)
    for index, number in enumerate(phase.beat_numbers):
        if beats[index] in {beats[1], beats[2]}:
            assert number != 1


def test_one_missed_downbeat_stays_established_with_visible_disagreement() -> None:
    beats = _grid(20)
    downbeats = [beats[i] for i in (0, 4, 9, 13, 17)]

    phase = lock_bar_phase(beats, downbeats)

    assert phase.bar_phase_unestablished is False
    assert all(1 <= n <= BAR_BEATS for n in phase.beat_numbers)
    assert phase.phase_agreement is not None
    assert phase.phase_agreement < 1.0
    assert phase.n_phase_disagreements >= 1


def test_pickup_numbers_leading_beats_from_chosen_phase() -> None:
    beats = _grid(12)
    downbeats = [beats[i] for i in (1, 5, 9)]

    phase = lock_bar_phase(beats, downbeats)

    assert phase.bar_phase_unestablished is False
    assert phase.chosen_phase == 1
    assert phase.beat_numbers[0] == 4


def test_phase_tie_breaks_to_smallest_index() -> None:
    beats = _grid(24)
    tie_downbeats = [beats[i] for i in (0, 6, 12, 18)]
    control_downbeats = tie_downbeats + [beats[22]]

    tie = lock_bar_phase(beats, tie_downbeats)
    control = lock_bar_phase(beats, control_downbeats)

    assert tie.chosen_phase == 0
    assert control.chosen_phase == 2
    assert tie.beat_numbers != control.beat_numbers


def test_below_floor_is_unestablished_with_empty_numbers() -> None:
    beats = _grid(24)
    downbeats = [beats[i] for i in (0, 6, 12, 18)]

    phase = lock_bar_phase(beats, downbeats, floor=0.51)

    assert phase.bar_phase_unestablished is True
    assert phase.reason == REASON_BAR_PHASE_BELOW_FLOOR
    assert phase.beat_numbers == []


def test_no_beats_and_no_downbeats_match_assign_bar_phase_reasons() -> None:
    assert lock_bar_phase([], [0.0]).reason == REASON_NO_BEATS
    assert lock_bar_phase(_grid(8), []).reason == REASON_NO_DOWNBEAT_ANCHOR


def test_downbeat_not_on_a_beat_raises() -> None:
    beats = _grid(8)
    with pytest.raises(ValueError, match="not a beat"):
        lock_bar_phase(beats, [beats[0] + 0.01])


def test_missed_downbeat_at_floor_zero_is_established_negative_control() -> None:
    beats = _grid(16)
    downbeats = [beats[i] for i in (0, 8, 12)]

    phase = lock_bar_phase(beats, downbeats, floor=0.0)

    assert phase.bar_phase_unestablished is False
    assert phase.beat_numbers


def test_production_floor_is_not_zero() -> None:
    assert BAR_PHASE_AGREEMENT_FLOOR > 0.0


@pytest.mark.requirement("BEATMAP-02")
def test_half_bar_doubles_before_the_bar_one_do_not_pick_the_phase() -> None:
    # Bar-1 on beats 4, 8, ... 28 (7 marks) and a beat-3 double on 2 .. 18 (5):
    # the first mark is a double. The old forward thinning kept 2, 6, 10, 14,
    # 18, 24, 28 and voted phase 2, five to two.
    beats = _grid(32)
    downbeats = [beats[i] for i in (2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 24, 28)]

    phase = lock_bar_phase(beats, downbeats)

    assert phase.bar_phase_unestablished is False
    assert phase.chosen_phase == 0
    assert phase.phase_agreement == 1.0
    assert phase.n_downbeats_thinned == 5
    assert phase.n_phase_disagreements == 0


@pytest.mark.requirement("BEATMAP-02")
def test_majority_phase_wins_whichever_mark_comes_first_control() -> None:
    # Same pattern with the doubles in the MAJORITY: the vote must follow the
    # majority there too, or the fix would just be "prefer the later mark".
    beats = _grid(32)
    downbeats = [beats[i] for i in (2, 4, 6, 10, 14, 18, 22, 26)]

    phase = lock_bar_phase(beats, downbeats)

    assert phase.chosen_phase == 2


@pytest.mark.requirement("BEATMAP-02")
def test_a_downbeat_on_every_beat_fails_closed() -> None:
    # Per-bar agreement alone would score this stream 1.0: every bar holds a
    # chosen-phase downbeat. The doubles-outnumber-bars rule must catch it.
    beats = _grid(32)

    phase = lock_bar_phase(beats, beats)

    assert phase.bar_phase_unestablished is True
    assert phase.reason == REASON_BAR_PHASE_BELOW_FLOOR
    assert phase.phase_agreement == pytest.approx(1 / BAR_BEATS)


@pytest.mark.requirement("BEATMAP-02")
def test_two_marks_per_bar_is_still_established_boundary() -> None:
    # Exactly one double per bar-1 is the half-bar pattern, not a stream.
    beats = _grid(32)
    downbeats = [beats[i] for i in range(0, 32, 2)]

    phase = lock_bar_phase(beats, downbeats)

    assert phase.bar_phase_unestablished is False
    assert phase.chosen_phase == 0
    assert phase.phase_agreement == 1.0
    assert phase.n_downbeats_thinned == 8


@pytest.mark.requirement("BEATMAP-02")
def test_a_mark_three_beats_after_bar_one_is_a_disagreement_not_a_double() -> None:
    vote = vote_bar_phase([0, 4, 8, 11, 12, 16])

    assert vote.chosen == 0
    assert vote.n_doubles == 1  # 11 is one beat before 12
    assert vote.n_disagreements == 0

    vote = vote_bar_phase([0, 4, 8, 12, 15, 20])

    assert vote.n_doubles == 0  # 15 is three from 12 and five from 20
    assert vote.n_disagreements == 1
    assert vote.agreement == pytest.approx(5 / 6)


@pytest.mark.requirement("BEATMAP-02")
def test_vote_bar_phase_refuses_no_downbeats() -> None:
    with pytest.raises(ValueError, match="at least one"):
        vote_bar_phase([])
