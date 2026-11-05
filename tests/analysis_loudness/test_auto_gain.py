"""Regression tests for the pure auto gain resolver (AUTOGAIN-01..05).

[if] a track is resolved [then] offset is target minus LUFS, peak-capped, [else stop].

Each test name reads "if <case> then broken" in the house regression format.
"""

from __future__ import annotations

import math

import pytest

from apps.analysis_loudness.http import auto_gain

TARGET = -12.0

pytestmark = pytest.mark.requirement("AUTOGAIN-01")


def _resolve(**overrides: object) -> auto_gain.AutoGain:
    kwargs: dict[str, object] = {
        "enabled": True,
        "target_lufs": TARGET,
        "integrated_lufs": -6.0,
        "true_peak_dbtp": 1.5,
        "user_offset_db": None,
    }
    kwargs.update(overrides)
    return auto_gain.compute_auto_gain(**kwargs)  # type: ignore[arg-type]


def test_offset_is_target_minus_measured_loudness() -> None:
    """If a -6 LUFS track does not get -6 dB at a -12 LUFS target then broken."""
    got = _resolve()
    assert got.status == "analysed"
    assert got.effective_offset_db == pytest.approx(-6.0)
    assert got.linear_gain == pytest.approx(10 ** (-6 / 20))
    assert got.limited_by is None


def test_cuts_are_never_peak_limited() -> None:
    """If a loud track's cut is shrunk by the true-peak rule then broken."""
    got = _resolve(integrated_lufs=-4.0, true_peak_dbtp=6.0)
    assert got.effective_offset_db == pytest.approx(-8.0)


def test_offset_is_clamped_to_range() -> None:
    """If a -36 LUFS track gets more than OFFSET_MAX_DB then broken."""
    got = _resolve(integrated_lufs=-36.0, true_peak_dbtp=-30.0)
    assert got.effective_offset_db == auto_gain.OFFSET_MAX_DB
    assert got.limited_by == "range"
    loud = _resolve(integrated_lufs=5.0, true_peak_dbtp=9.0)
    assert loud.effective_offset_db == auto_gain.OFFSET_MIN_DB


def test_quiet_track_boost_stops_at_true_peak_ceiling() -> None:
    """If a boosted quiet track's true peak ends above the ceiling then broken (no clipping)."""
    got = _resolve(integrated_lufs=-15.0, true_peak_dbtp=-2.0)
    assert got.effective_offset_db == pytest.approx(1.0)
    assert got.limited_by == "true_peak"
    assert -2.0 + got.effective_offset_db <= auto_gain.TRUE_PEAK_CEILING_DBTP + 1e-9


def test_true_peak_rule_never_turns_a_boost_into_a_cut() -> None:
    """If a quiet track already peaking above the ceiling is cut then broken."""
    got = _resolve(integrated_lufs=-14.0, true_peak_dbtp=0.5)
    assert got.effective_offset_db == 0.0
    assert got.limited_by == "true_peak"


def test_boost_without_a_true_peak_measurement_is_refused() -> None:
    """If a track with no measured true peak is boosted then broken."""
    got = _resolve(integrated_lufs=-20.0, true_peak_dbtp=None)
    assert got.effective_offset_db == 0.0


def test_no_analysis_is_zero_db_and_says_so() -> None:
    """If an unanalysed track gets a guessed offset, or no visible state, then broken."""
    got = _resolve(integrated_lufs=None, true_peak_dbtp=None)
    assert got.status == "not_analysed"
    assert got.effective_offset_db == 0.0
    assert got.analysed_offset_db is None
    assert _resolve(integrated_lufs=math.nan).status == "not_analysed"


def test_user_offset_wins_over_analysis() -> None:
    """If the user's own offset does not beat the analysed one then broken."""
    got = _resolve(user_offset_db=-2.0)
    assert got.status == "user"
    assert got.effective_offset_db == -2.0
    assert got.analysed_offset_db == pytest.approx(-6.0), "the analysed value sits beside it"
    unanalysed = _resolve(integrated_lufs=None, user_offset_db=3.0)
    assert (unanalysed.status, unanalysed.effective_offset_db) == ("user", 3.0)


def test_off_is_zero_db_whatever_else_is_set() -> None:
    """If auto gain switched off still moves the level then broken."""
    got = _resolve(enabled=False, user_offset_db=-2.0)
    assert (got.status, got.effective_offset_db) == ("off", 0.0)


@pytest.mark.parametrize("bad", [-30.0, 0.0, math.inf])
def test_out_of_range_target_raises(bad: float) -> None:
    """If an impossible target is accepted silently then broken."""
    with pytest.raises(ValueError, match="target"):
        _resolve(target_lufs=bad)


@pytest.mark.parametrize("bad", [-13.0, 12.5, math.nan])
def test_out_of_range_user_offset_raises(bad: float) -> None:
    """If an out-of-range user offset is accepted silently then broken."""
    with pytest.raises(ValueError, match="user offset"):
        _resolve(user_offset_db=bad)


def test_spread_shrinks_to_one_lu_on_a_wide_set() -> None:
    """If tracks 10 LU apart are not within 1 LU after auto gain then broken."""
    tracks = [(-5.5, 2.0), (-8.0, 1.0), (-11.0, -0.5), (-13.0, -3.5), (-15.5, -5.0)]
    after = [lufs + _resolve(integrated_lufs=lufs, true_peak_dbtp=tp).effective_offset_db for lufs, tp in tracks]
    assert max(lufs for lufs, _ in tracks) - min(lufs for lufs, _ in tracks) == pytest.approx(10.0)
    assert max(after) - min(after) <= 1.0
