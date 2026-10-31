"""A beatgrid record from an older producer is re-run by the ahead drain (NATIVE-34).

[if] a stored grid or failure predates the producer [then] the drain re-runs it, [else stop].
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.analysis import store as analysis_store
from apps.analysis.lanes import LaneResult
from apps.analysis.record import AnalysisRecord
from apps.analysis_beatgrid.version import PRODUCER_VERSION
from apps.webui.server.ahead_analysis_records import declined_ids, done_ids

pytestmark = pytest.mark.requirement("NATIVE-34")

BACKEND = "own_beatgrid.backfill"
OLD_VERSION = "1.6.0"
REASON = "grid_fit_bar_phase_below_floor"
STAMP = "2026-10-08T00:00:00Z"


def _track(conn, stable_id: str) -> None:
    conn.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, file_path, "
        "audio_hash, created_at, updated_at) VALUES (?, 'inferred', ?, ?, ?, ?, ?)",
        (stable_id, stable_id, f"/music/{stable_id}.wav", "sha256:" + "ab" * 32, STAMP, STAMP),
    )


def _failed(stable_id: str, version: str) -> AnalysisRecord:
    return AnalysisRecord(
        stable_id=stable_id,
        backend=BACKEND,
        backend_version=version,
        analyzed_at=datetime(2026, 10, 8, tzinfo=UTC),
        duration_s=30.0,
        sample_rate=44100,
        bpm=120.0,
        bpm_confidence=0.9,
        key_camelot="8A",
        key_openkey="1m",
        key_confidence=0.8,
        energy=5,
        producer="backfill",
        producer_version=version,
        uses_model=False,
        model_sha256=None,
        decode_fingerprint="sha256:" + "66" * 32,
        lanes={"beatgrid": LaneResult(status="failed", reason=REASON, payload={})},
    )


def _ok(stable_id: str, version: str) -> AnalysisRecord:
    payload = {
        "beats": [{"t": 0.0, "n": 1, "bpm": 124.0}, {"t": 0.48, "n": 2, "bpm": 124.0}],
        "bpm": 124.0,
        "bpm_confidence": 0.9,
        "octave_reason": "single_octave_in_range",
        "first_downbeat_s": 0.0,
        "tempo_changes": [],
        "static_grid_untrusted": False,
    }
    return dataclasses.replace(
        _failed(stable_id, version),
        bpm=124.0,
        lanes={"beatgrid": LaneResult(status="ok", reason=None, payload=payload)},
    )


def test_an_older_grid_and_failure_are_requeued_and_current_ones_are_not(tmp_path: Path) -> None:
    """[if] a record predates the producer [then] it is not done, a current one is, [else stop]."""
    assert PRODUCER_VERSION != OLD_VERSION
    path = tmp_path / "state.db"
    conn = analysis_store.open_conn(path)
    try:
        for sid in ("old-grid", "old-fail", "now-grid", "now-fail"):
            _track(conn, sid)
        conn.commit()
    finally:
        conn.close()
    for record in (
        _ok("old-grid", OLD_VERSION),
        _failed("old-fail", OLD_VERSION),
        _ok("now-grid", PRODUCER_VERSION),
        _failed("now-fail", PRODUCER_VERSION),
    ):
        analysis_store.upsert_record(record, db_path=path)

    def opener():
        return analysis_store.open_conn(path)

    assert done_ids(opener, BACKEND) == {"now-grid", "now-fail"}
    assert declined_ids(opener, "beatgrid", BACKEND) == {"now-fail": REASON}
