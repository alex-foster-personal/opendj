"""Target energy curve for PLAY IT."""
from __future__ import annotations

from .set_goal import SetGoal


def target_energy_at(goal: SetGoal, position_min: float) -> float:
    """Piecewise-linear target energy at ``position_min`` minutes."""
    clamped = max(0.0, min(float(position_min), float(goal.duration_min)))

    if goal.peak_at_min is None:
        return (float(goal.floor_energy) + float(goal.ceiling_energy)) / 2.0

    close = (
        float(goal.floor_energy)
        if goal.close_on_energy is None
        else float(goal.close_on_energy)
    )
    peak = float(goal.ceiling_energy)
    floor = float(goal.floor_energy)
    peak_t = float(goal.peak_at_min)
    end_t = float(goal.duration_min)

    if clamped <= peak_t:
        if peak_t == 0.0:
            return peak
        frac = clamped / peak_t
        return floor + (peak - floor) * frac

    if end_t == peak_t:
        return close
    frac = (clamped - peak_t) / (end_t - peak_t)
    return peak + (close - peak) * frac
