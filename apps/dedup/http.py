"""Delivery-layer entry point for ``apps.dedup`` (ADR-NEW-module-contracts, M1)."""

from __future__ import annotations

from .contract import (
    CONTRACT_VERSION,
    DEFAULT_DURATION_DELTA_S,
    DEFAULT_MAX_CLUSTER,
)
from .contract.models import DedupService, ScanStatus
from .service import LocalDedupService


def build_service() -> DedupService:
    """The dedup service delivery code (webui, CLI, scripts) talks to."""
    return LocalDedupService()


__all__ = [
    "CONTRACT_VERSION",
    "DEFAULT_DURATION_DELTA_S",
    "DEFAULT_MAX_CLUSTER",
    "DedupService",
    "ScanStatus",
    "build_service",
]
