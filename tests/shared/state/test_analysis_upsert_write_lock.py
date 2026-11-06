"""STATE-15: analysis upserts take the writer lock before their first read.

Sentry OPEN-DJ-BE-7A: ``OperationalError: database is locked`` from a deferred
SELECT then SAVEPOINT on an idle analysis connection.

[if] a peer commits during analysis upsert [then] the row still lands, [else stop].
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.analysis.record import AnalysisRecord
from apps.analysis.store import fetch_records_by_ids, open_conn, upsert_record
from apps.shared.state import db as state_db

pytestmark = pytest.mark.requirement("STATE-15")


def _rec(sid: str = "sidLock") -> AnalysisRecord:
    return AnalysisRecord(
        stable_id=sid,
        backend="librosa+madmom",
        backend_version="test-1.0",
        analyzed_at=datetime(2026, 4, 17, tzinfo=UTC),
        duration_s=60.0,
        sample_rate=44100,
        bpm=120.0,
        bpm_confidence=0.9,
        key_camelot="8A",
        key_openkey="8m",
        key_confidence=0.9,
        energy=6,
    )


def test_an_analysis_upsert_survives_a_commit_between_its_first_read_and_first_write(
    tmp_path: Path,
) -> None:
    """[if] a peer commits during analysis upsert [then] the row still lands, [else stop]."""
    db = tmp_path / "state.db"
    conn = open_conn(db)
    attempts = {"n": 0}
    blocked: list[str] = []

    def inject(_sql: str) -> None:
        if "SELECT record_json FROM analysis" not in _sql:
            return
        attempts["n"] += 1
        if attempts["n"] > 1:
            return
        peer = state_db.open_rw(db, apply_schema=False, busy_timeout_s=0.05)
        try:
            peer.execute(
                "INSERT OR IGNORE INTO schema_meta_markers(marker, applied_at) VALUES (?, ?)",
                ("test-analysis-upsert-lock", "2026-10-05T00:00:00+00:00"),
            )
        except sqlite3.OperationalError as exc:
            if not state_db.is_sqlite_busy(exc):
                raise
            blocked.append(getattr(exc, "sqlite_errorname", str(exc)))
        finally:
            peer.close()

    try:
        conn.set_trace_callback(inject)
        result = upsert_record(_rec(), db_path=db, conn=conn)
    finally:
        conn.set_trace_callback(None)
        conn.close()

    assert attempts["n"] >= 1, "the upsert never issued its lookup, so nothing was tested"
    assert blocked, (
        "the concurrent commit went through mid-upsert: the unit was not holding "
        "the writer lock, so it was exposed to SQLITE_BUSY_SNAPSHOT"
    )
    assert result.inserted and not result.unchanged
    back = fetch_records_by_ids(["sidLock"], db_path=db)
    assert len(back) == 1
    peer = state_db.open_rw(db, apply_schema=False)
    try:
        peer.execute(
            "INSERT OR IGNORE INTO schema_meta_markers(marker, applied_at) VALUES (?, ?)",
            ("test-analysis-upsert-lock", "2026-10-05T00:00:00+00:00"),
        )
        count = peer.execute(
            "SELECT COUNT(*) FROM schema_meta_markers WHERE marker = ?",
            ("test-analysis-upsert-lock",),
        ).fetchone()
    finally:
        peer.close()
    assert count is not None and int(count[0]) == 1
