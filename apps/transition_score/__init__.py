"""Transition scoring from rendered audio, and the try-er (TRYER-01..05).

Import surface for the endpoint: :func:`try_pair`, :class:`TryResult`,
:class:`Candidate`, :data:`SCORER_VERSION`.
"""

from apps.transition_score.contract import (
    CORE_FACTORS,
    MIN_CORE_MEASURED,
    SCORER_VERSION,
    Candidate,
    Measured,
    ScoreResult,
    TryResult,
    Veto,
    best_candidate,
)
from apps.transition_score.tryer import try_pair

__all__ = [
    "CORE_FACTORS",
    "MIN_CORE_MEASURED",
    "SCORER_VERSION",
    "Candidate",
    "Measured",
    "ScoreResult",
    "TryResult",
    "Veto",
    "best_candidate",
    "try_pair",
]
