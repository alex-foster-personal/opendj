"""Delivery-layer entry point for ``apps.analysis_structure`` (ADR-NEW-module-contracts).

Delivery code (``apps.webui``, ``apps.engine_core``, ``apps.opendj_cli``) and
``scripts/`` build the service here; everything else imports only the contract.

-Claude
"""

from __future__ import annotations

from .contract import CONTRACT_VERSION, PHRASE_SOURCE_LANES, StructureService
from .service import LocalStructureService


def build_service() -> StructureService:
    return LocalStructureService()


__all__ = ["CONTRACT_VERSION", "PHRASE_SOURCE_LANES", "StructureService", "build_service"]
