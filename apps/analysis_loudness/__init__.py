"""Native-analysis loudness producer over :mod:`apps.loudness`.

Outside this package, import :mod:`apps.analysis_loudness.contract` (delivery
layers may also use :mod:`apps.analysis_loudness.http`). This module stays
pure: it re-exports the contract only.
"""

from .contract import CONTRACT_VERSION, PRODUCER_BACKEND, PRODUCER_VERSION, LoudnessAnalysisService

__all__ = [
    "CONTRACT_VERSION",
    "PRODUCER_BACKEND",
    "PRODUCER_VERSION",
    "LoudnessAnalysisService",
]
