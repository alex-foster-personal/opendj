"""Delivery-layer entry point for :mod:`apps.analysis_loudness`.

The delivery layers (apps.webui, apps.engine_core, apps.opendj_cli) and
scripts/ build the service here; constants come from the contract. The
pure auto gain resolver (:mod:`apps.analysis_loudness.auto_gain`) is served
from here too.
"""

from __future__ import annotations

from . import auto_gain
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
    "auto_gain",
    "build_service",
]
