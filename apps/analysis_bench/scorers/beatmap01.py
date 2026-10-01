"""BEATMAP-01 locked agreement floors (issue #2351), re-measured in round 6.

These constants are the ratcheted record of the 90-track subset90 measure.
Tests import them; do not loosen without a new measured round.

Round 6 (Tue 29 Sep 2026, `ops/beatbench/round-6/bar-phase-gate.json`, written
by `python -m scripts.beatbench.bar_phase_gate`): `bar_phase.vote_bar_phase`
votes on every downbeat instead of thinning first. The ok count went 72 -> 69
and the fixed downbeat floor 0.705 -> 0.7319. The ok count is the one number
that fell, and it fell for the right reason: of the tracks whose outcome
changed, the ones that now fail closed agreed with rekordbox's bar-1 on 0,
6, 7, 15 and 50 percent, while two fixed tracks went from 0 to 100 percent.
"""

from __future__ import annotations

from apps.analysis_beatgrid.bar_phase import BAR_PHASE_AGREEMENT_FLOOR
from apps.analysis_beatgrid.version import PRODUCER_VERSION
from apps.analysis_bench.scorers.beatgrid import SCORER_VERSION

# Measured ok count on ops/beatbench/round-1/raw-beatgrid_lane_t050.json at
# producer 1.2.0 with lock_bar_phase (round 6 vote). Ratchet: later changes may
# not go below.
BEATMAP01_SUBSET90_OK_MIN = 69

# Mean served n==1 agreement on fixed-partition ok rows (denominator: fixed ok).
BEATMAP01_FIXED_DOWNBEAT_MIN = 0.7319

__all__ = [
    "BAR_PHASE_AGREEMENT_FLOOR",
    "BEATMAP01_FIXED_DOWNBEAT_MIN",
    "BEATMAP01_SUBSET90_OK_MIN",
    "PRODUCER_VERSION",
    "SCORER_VERSION",
]
