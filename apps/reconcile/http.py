"""Delivery-layer entry point for ``apps.reconcile`` (ADR-NEW-module-contracts).

``apps.webui``, ``apps.engine_core``, ``apps.opendj_cli`` and ``scripts/``
build the service here; everything else imports ``apps.reconcile.contract``.
"""

from __future__ import annotations

from pathlib import Path

from apps.reconcile.contract import CONTRACT_VERSION, ReconcileService
from apps.reconcile.source_flags import flags_by_stable_id, read_store, scan, write_store


def build_service(*, music_roots: list[Path] | None = None, dedup_db: Path | None = None) -> ReconcileService:
    """The in-process reconcile service, over the app's configured roots unless given others."""
    from apps.reconcile.service import LocalReconcileService

    return LocalReconcileService(music_roots=music_roots, dedup_db=dedup_db)


# SRCFLAG-01: the source-flag scan and store, re-exported for the delivery layers.
__all__ = ["CONTRACT_VERSION", "build_service", "flags_by_stable_id", "read_store", "scan", "write_store"]
