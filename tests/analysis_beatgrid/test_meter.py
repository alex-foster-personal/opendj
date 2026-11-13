"""Beats-per-bar estimation from downbeat spacing."""

from __future__ import annotations

import pytest

from apps.analysis_beatgrid.meter import (
    COMPOUND_METER_NOTE_6_8,
    DEFAULT_BEATS_PER_BAR,
    STATUS_MEASURED,
    STATUS_UNMEASURED,
    estimate_beats_per_bar,
)


def _grid(n_beats: int, period_s: float = 0.5) -> list[float]:
    return [round(i * period_s, 6) for i in range(n_beats)]


def _downbeats_every(beats: list[float], step: int) -> list[float]:
    return [beats[i] for i in range(0, len(beats), step)]


@pytest.mark.requirement("METER-12679")
def test_synthetic_three_four_spacing_is_measured_three():
    """[if] downbeats land every 3 beats [then] beats_per_bar is 3 and measured, [else stop]."""
    beats = _grid(24)
    downbeats = _downbeats_every(beats, 3)

    meter = estimate_beats_per_bar(beats, downbeats)

    assert meter.beats_per_bar == 3
    assert meter.beats_per_bar_status == STATUS_MEASURED


@pytest.mark.requirement("METER-12679")
def test_synthetic_six_eight_spacing_is_measured_six_not_silent_four():
    """[if] downbeats land every 6 beats [then] beats_per_bar is 6 measured, [else stop]."""
    beats = _grid(36)
    downbeats = _downbeats_every(beats, 6)

    meter = estimate_beats_per_bar(beats, downbeats)

    assert meter.beats_per_bar == 6
    assert meter.beats_per_bar_status == STATUS_MEASURED
    assert meter.compound_meter_note is None


@pytest.mark.requirement("METER-12679")
def test_six_eight_estimator_output_reports_six_with_spacing_vote_and_no_compound_note():
    """[if] spacing vote is 6 [then] real output is 6 measured with no compound note, [else stop]."""
    beats = _grid(36)
    downbeats = _downbeats_every(beats, 6)

    meter = estimate_beats_per_bar(beats, downbeats)

    assert meter.beats_per_bar == 6
    assert meter.beats_per_bar_status == STATUS_MEASURED
    assert meter.spacing_vote == 6
    assert meter.n_closed_bars == 5
    assert meter.compound_meter_note is None
    assert meter.compound_meter_note != COMPOUND_METER_NOTE_6_8


@pytest.mark.requirement("METER-12679")
def test_synthetic_four_four_spacing_is_measured_four():
    """[if] downbeats land every 4 beats [then] beats_per_bar is 4 and measured, [else stop]."""
    beats = _grid(32)
    downbeats = _downbeats_every(beats, 4)

    meter = estimate_beats_per_bar(beats, downbeats)

    assert meter.beats_per_bar == 4
    assert meter.beats_per_bar_status == STATUS_MEASURED


@pytest.mark.requirement("METER-12679")
def test_irregular_spacing_is_unmeasured_with_default_four():
    """[if] closed bar lengths tie without a majority [then] status is unmeasured at 4, [else stop]."""
    beats = _grid(20)
    downbeats = [beats[i] for i in (0, 4, 7, 11, 14)]

    meter = estimate_beats_per_bar(beats, downbeats)

    assert meter.beats_per_bar == DEFAULT_BEATS_PER_BAR
    assert meter.beats_per_bar_status == STATUS_UNMEASURED


@pytest.mark.requirement("METER-12679")
def test_missing_downbeats_is_unmeasured_with_default_four():
    """[if] fewer than two downbeats anchor the grid [then] status is unmeasured at 4, [else stop]."""
    beats = _grid(16)

    meter = estimate_beats_per_bar(beats, [])

    assert meter.beats_per_bar == DEFAULT_BEATS_PER_BAR
    assert meter.beats_per_bar_status == STATUS_UNMEASURED
