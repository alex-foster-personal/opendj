"""Bridge from :mod:`apps.analysis` to the shared state layer.

Thin wrapper over :mod:`apps.shared.state.analysis_shim` until Phase 5
ships a canonical ``apps.shared.state.analysis`` module.  See
06-01-PLAN §Step 5.

Once Phase 5 lands:

1. Flip the imports here.
2. Delete ``apps/shared/state/analysis_shim.py``.
3. Keep this module's public signatures stable so callers do not change.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

# TODO(phase-05): replace with from apps.shared.state import analysis, events.
from apps.shared.state import analysis_shim

from .record import AnalysisRecord


def upsert_record(
    record: AnalysisRecord,
    *,
    db_path: Path | None = None,
) -> analysis_shim.UpsertResult:
    """Persist ``record``; emit an ``analyze`` event on insert/update."""
    result = analysis_shim.upsert(record, db_path=db_path)
    if not result.unchanged:
        analysis_shim.publish(
            event_type="analyze",
            payload={
                "stable_id": record.stable_id,
                "backend": record.backend,
                "backend_version": record.backend_version,
                "analyzed_at": record.analyzed_at.isoformat().replace("+00:00", "Z"),
                "bpm": record.bpm,
                "key_camelot": record.key_camelot,
                "energy": record.energy,
                "inserted": result.inserted,
            },
            stable_id=record.stable_id,
            db_path=db_path,
        )
    return result


def publish_event(
    event_type: str,
    payload: dict[str, Any],
    *,
    stable_id: str | None = None,
    db_path: Path | None = None,
) -> int:
    """Generic event passthrough (used by 06-02 consumers)."""
    return analysis_shim.publish(
        event_type=event_type,
        payload=payload,
        stable_id=stable_id,
        db_path=db_path,
    )


def fetch_records_by_ids(
    stable_ids: list[str],
    *,
    backend: str | None = None,
    db_path: Path | None = None,
) -> list[AnalysisRecord]:
    """Reconstruct :class:`AnalysisRecord` objects."""
    rows = analysis_shim.fetch_records(
        stable_ids=stable_ids, backend=backend, db_path=db_path
    )
    return [AnalysisRecord.from_json(r["record_json"]) for r in rows]


__all__ = [
    "upsert_record",
    "publish_event",
    "fetch_records_by_ids",
]
