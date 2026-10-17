"""Delivery-layer entry point for :mod:`apps.analysis_loudness`.

The delivery layers (apps.webui, apps.engine_core, apps.opendj_cli) and
scripts/ build the service here; constants come from the contract.
"""

from __future__ import annotations

from .contract import CONTRACT_VERSION, PRODUCER_BACKEND, PRODUCER_VERSION, LoudnessAnalysisService
from .service import LoudnessAnalysisServiceImpl


def build_service() -> LoudnessAnalysisService:
    """The loudness analysis service, ready to call."""
    return LoudnessAnalysisServiceImpl()


__all__ = [
    "CONTRACT_VERSION",
    "PRODUCER_BACKEND",
    "PRODUCER_VERSION",
    "LoudnessAnalysisService",
    "build_service",
]
