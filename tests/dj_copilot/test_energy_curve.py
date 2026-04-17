"""Energy curve behaviour (PLAY-02 / AI-02)."""
from __future__ import annotations

import pytest

from apps.dj_copilot.energy_curve import target_energy_at
from apps.dj_copilot.set_goal import SetGoal

pytestmark = [
    pytest.mark.requirement("PLAY-02"),
    pytest.mark.requirement("AI-02"),
]


def test_flat_when_no_peak() -> None:
    g = SetGoal(duration_min=60, floor_energy=4, ceiling_energy=8)
    assert target_energy_at(g, 0.0) == 6.0
    assert target_energy_at(g, 30.0) == 6.0
    assert target_energy_at(g, 60.0) == 6.0


def test_peak_and_close() -> None:
    g = SetGoal(
        duration_min=90,
        peak_at_min=45,
        floor_energy=3,
        ceiling_energy=9,
        close_on_energy=4,
    )
    assert target_energy_at(g, 0.0) == pytest.approx(3.0)
    assert target_energy_at(g, 45.0) == pytest.approx(9.0)
    assert target_energy_at(g, 90.0) == pytest.approx(4.0)
    assert target_energy_at(g, 22.5) == pytest.approx(6.0)


def test_peak_default_close_is_floor() -> None:
    g = SetGoal(duration_min=60, peak_at_min=30, floor_energy=2, ceiling_energy=8)
    assert target_energy_at(g, 60.0) == pytest.approx(2.0)


def test_clamp_out_of_range() -> None:
    g = SetGoal(duration_min=60, peak_at_min=30, floor_energy=2, ceiling_energy=8)
    assert target_energy_at(g, -5.0) == target_energy_at(g, 0.0)
    assert target_energy_at(g, 999.0) == target_energy_at(g, 60.0)
