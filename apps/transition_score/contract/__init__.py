"""The public surface of ``apps.transition_score`` (ADR-NEW-module-contracts).

Code outside ``apps.transition_score`` imports only this package (and, from a delivery
layer or script, ``apps.transition_score.http``). The contract imports only the standard
library, pydantic and other contracts.
"""

from __future__ import annotations

from .models import (
    CORE_FACTORS,
    MIN_CORE_MEASURED,
    RANK_ALIGNMENT_UNKNOWN,
    RANK_MEASURED,
    RANK_REFUSED,
    SCORER_VERSION,
    VOCAL_CUT_GAIN_DB,
    VOCAL_CUT_WINDOW_S,
    Candidate,
    Measured,
    ScoreResult,
    TryResult,
    Veto,
    best_candidate,
    core_measured,
    rank_key,
)
from .service import TransitionScoreService

CONTRACT_VERSION = "transition_score/1"

__all__ = [
    "CONTRACT_VERSION",
    "CORE_FACTORS",
    "MIN_CORE_MEASURED",
    "RANK_ALIGNMENT_UNKNOWN",
    "RANK_MEASURED",
    "RANK_REFUSED",
    "SCORER_VERSION",
    "VOCAL_CUT_GAIN_DB",
    "VOCAL_CUT_WINDOW_S",
    "Candidate",
    "Measured",
    "ScoreResult",
    "TransitionScoreService",
    "TryResult",
    "Veto",
    "best_candidate",
    "core_measured",
    "rank_key",
]
