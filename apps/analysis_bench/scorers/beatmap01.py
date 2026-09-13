"""BEATMAP-01 locked agreement floors from round 3 (issue #2351).

These constants are the ratcheted record of the 90-track subset90 measure.
Tests import them; do not loosen without a new measured round.
"""

from __future__ import annotations

from apps.analysis_beatgrid.bar_phase import BAR_PHASE_AGREEMENT_FLOOR
from apps.analysis_beatgrid.version import PRODUCER_VERSION
from apps.analysis_bench.scorers.beatgrid import SCORER_VERSION

# Measured ok count on ops/beatbench/round-1/raw-beatgrid_lane_t050.json at
# producer 1.2.0 with lock_bar_phase. Ratchet: later changes may not go below.
BEATMAP01_SUBSET90_OK_MIN = 72

# Mean served n==1 agreement on fixed-partition ok rows (denominator: fixed ok).
BEATMAP01_FIXED_DOWNBEAT_MIN = 0.705

__all__ = [
    "BAR_PHASE_AGREEMENT_FLOOR",
    "BEATMAP01_FIXED_DOWNBEAT_MIN",
    "BEATMAP01_SUBSET90_OK_MIN",
    "PRODUCER_VERSION",
    "SCORER_VERSION",
]
