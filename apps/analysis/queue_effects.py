"""Side effects every analysis record write may trigger (NATIVE-10).

Kept out of :mod:`apps.analysis.store` so the persistence bridge stays under
the file-size ratchet while cascade and version-bump requeue logic live in one
place both ``upsert_record`` and the queue runner can call.

-Claude
"""
from __future__ import annotations

import sqlite3

from . import queue_targets
from .lanes import parse_own_backend
from .queue import CascadeOutcome, cascade_dependents, requeue_for_version_bump
from .record import AnalysisRecord
from .store import UpsertResult


def cascade_if_canonical(
    conn: sqlite3.Connection, record: AnalysisRecord
) -> list[CascadeOutcome]:
    """Run the dependency cascade when ``record`` is the canonical row."""
    from .canonical import canonical_pointer

    parsed = parse_own_backend(record.backend)
    if parsed is None:
        return []
    if canonical_pointer(conn, record.stable_id, parsed.lane) != (
        record.backend,
        record.backend_version,
    ):
        return []
    return cascade_dependents(
        conn,
        stable_id=record.stable_id,
        lane=parsed.lane,
        dependency_record=record,
    )


def auto_requeue_on_version_bump(
    conn: sqlite3.Connection, record: AnalysisRecord, *, inserted: bool
) -> None:
    """Enqueue stale tracks when a NEW row lands at the current producer version."""
    if not inserted:
        return
    parsed = parse_own_backend(record.backend)
    if parsed is None:
        return
    requeue_for_version_bump(
        conn,
        backend=record.backend,
        current_version=record.backend_version,
        lane=parsed.lane,
        resolve=queue_targets.candidates_from_state,
    )


def apply_record_write_effects(
    conn: sqlite3.Connection,
    record: AnalysisRecord,
    result: UpsertResult,
    *,
    cascade: bool,
    version_bump: bool,
) -> list[CascadeOutcome]:
    """Post-persist hooks shared by ``upsert_record`` and tests."""
    if result.unchanged:
        return []
    outcomes: list[CascadeOutcome] = []
    if cascade:
        outcomes = cascade_if_canonical(conn, record)
    if version_bump:
        auto_requeue_on_version_bump(conn, record, inserted=result.inserted)
    return outcomes


__all__ = [
    "apply_record_write_effects",
    "auto_requeue_on_version_bump",
    "cascade_if_canonical",
]
