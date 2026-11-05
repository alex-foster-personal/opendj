"""A folder rewrite drops only analysis the drain can remake (NATIVE-30).

[if] a file is rewritten [then] drain rows drop and other rows stay, stale, [else stop].
"""

from __future__ import annotations

import os
import struct
import wave
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.analysis.lanes import LaneResult
from apps.analysis.record import AnalysisRecord
from apps.analysis.store import upsert_record
from apps.shared.state.analysis_stale import STALE_AUDIO_REPLACED, WHOLE_RECORD
from apps.shared.state.events import FakeEventBus
from apps.shared.state.ingest import folder, folder_rescan
from apps.shared.state.writer import StateWriter
from apps.webui.server.ahead_analysis import LANE_ORDER

pytestmark = pytest.mark.requirement("NATIVE-30")

DRAIN_BACKEND = "own_beatgrid.backfill"
IMPORTED_BACKEND = "mik"
IMPORTED_VERSION = "10.0"


def _write_wav_sample(path: Path, sample: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(struct.pack("<h", sample))


def _record(stable_id: str, backend: str, version: str, lane: str, reason: str) -> AnalysisRecord:
    return AnalysisRecord(
        stable_id=stable_id,
        backend=backend,
        backend_version=version,
        analyzed_at=datetime(2026, 10, 8, tzinfo=UTC),
        duration_s=1.0,
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
        decode_fingerprint="sha256:" + "66" * 32,
        lanes={lane: LaneResult(status="failed", reason=reason, payload={})},
    )


def _analysed_file(state_conn, state_db_path: Path, root: Path) -> tuple[StateWriter, str, str]:
    """A scanned file holding one drain record and one imported record."""
    _write_wav_sample(root / "a.wav", 0)
    writer = StateWriter(state_conn, bus=FakeEventBus(), actor="rewrite-keeps-test")
    first = folder_rescan.reconcile_folders(writer, [root], previous_signature="")
    stable_id = state_conn.execute("SELECT stable_id FROM tracks WHERE deleted_at IS NULL").fetchone()[0]
    drain = _record(stable_id, DRAIN_BACKEND, "1.6.0", "beatgrid", "no_trackable_pulse")
    imported = _record(stable_id, IMPORTED_BACKEND, IMPORTED_VERSION, "key", "no_tonal_center")
    upsert_record(drain, db_path=state_db_path)
    upsert_record(imported, db_path=state_db_path)
    return writer, stable_id, first.signature


def _backends(state_conn, stable_id: str) -> list[str]:
    rows = state_conn.execute(
        "SELECT backend FROM analysis WHERE stable_id = ? ORDER BY backend", (stable_id,)
    ).fetchall()
    return [row[0] for row in rows]


def _stale(state_conn, stable_id: str) -> list[tuple[str, str, str, str]]:
    rows = state_conn.execute(
        "SELECT lane, backend, backend_version, reason FROM analysis_stale WHERE stable_id = ?",
        (stable_id,),
    ).fetchall()
    return [tuple(row) for row in rows]


def test_a_rewrite_drops_drain_analysis_and_keeps_imported_analysis_stale(
    state_conn, state_db_path: Path, tmp_path: Path
) -> None:
    """[if] the bytes change [then] the drain row drops, the mik row stays stale, [else stop]."""
    root = tmp_path / "music"
    writer, stable_id, signature = _analysed_file(state_conn, state_db_path, root)
    audio_path = root / "a.wav"

    _write_wav_sample(audio_path, 2000)
    os.utime(audio_path, (os.path.getmtime(audio_path) + 5, os.path.getmtime(audio_path) + 5))
    folder_rescan.reconcile_folders(writer, [root], previous_signature=signature)

    assert _backends(state_conn, stable_id) == [IMPORTED_BACKEND]
    assert sorted(_stale(state_conn, stable_id)) == [
        (WHOLE_RECORD, IMPORTED_BACKEND, IMPORTED_VERSION, STALE_AUDIO_REPLACED),
        ("key", IMPORTED_BACKEND, IMPORTED_VERSION, STALE_AUDIO_REPLACED),
    ]


def test_an_unchanged_file_keeps_both_records_fresh(state_conn, state_db_path: Path, tmp_path: Path) -> None:
    """[if] a rescan walks unchanged bytes [then] both rows stay and none is stale, [else stop]."""
    root = tmp_path / "music"
    writer, stable_id, _signature = _analysed_file(state_conn, state_db_path, root)

    folder_rescan.reconcile_folders(writer, [root], previous_signature="")

    assert _backends(state_conn, stable_id) == [IMPORTED_BACKEND, DRAIN_BACKEND]
    assert _stale(state_conn, stable_id) == []


def test_the_dropped_backends_are_exactly_the_drain_lanes() -> None:
    """[if] the drain gains or loses a lane [then] the rewrite drop set follows, [else stop]."""
    assert set(folder._DRAIN_REGENERATED_BACKENDS) == {backend for _lane, backend in LANE_ORDER}


def _legacy_routes(state_db_path: Path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from apps.webui.server.backend import InMemoryBackend
    from apps.webui.server.routes.analysis import router

    app = FastAPI()
    app.state.backend = InMemoryBackend()
    app.state.analysis_db_path = state_db_path
    app.state.anlz_available_fn = lambda _sid: False
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def _served(client, stable_id: str) -> tuple[int, int]:
    return (
        client.get(f"/api/v1/tracks/{stable_id}/beatgrid-fallback").status_code,
        client.get(f"/api/v1/tracks/{stable_id}/auto-cues").status_code,
    )


def test_a_rewrite_withholds_a_lane_less_legacy_record_from_the_latest_record_readers(
    state_conn, state_db_path: Path, tmp_path: Path
) -> None:
    """[if] the bytes change [then] a lane-less legacy record is not served until rewritten, [else stop].

    A pre-v2 record has no lanes to mark per lane, so only the whole-record
    marker withholds it from /beatgrid-fallback and /auto-cues. The controls:
    the record is served before the rewrite, and again once its producer
    writes it anew.
    """
    from tests.test_analysis_route import _record as legacy_record

    root = tmp_path / "music"
    _write_wav_sample(root / "a.wav", 0)
    writer = StateWriter(state_conn, bus=FakeEventBus(), actor="rewrite-keeps-test")
    signature = folder_rescan.reconcile_folders(writer, [root], previous_signature="").signature
    (stable_id,) = state_conn.execute("SELECT stable_id FROM tracks WHERE deleted_at IS NULL").fetchone()
    legacy = legacy_record(stable_id, duration_s=1.0, downbeats=[0.0, 0.5])
    assert not legacy.lanes, "the record under test is lane-less"
    upsert_record(legacy, db_path=state_db_path)
    with _legacy_routes(state_db_path) as client:
        assert _served(client, stable_id) == (200, 200), "served before the rewrite"

        audio_path = root / "a.wav"
        _write_wav_sample(audio_path, 2000)
        os.utime(audio_path, (os.path.getmtime(audio_path) + 5, os.path.getmtime(audio_path) + 5))
        folder_rescan.reconcile_folders(writer, [root], previous_signature=signature)

        assert legacy.backend in _backends(state_conn, stable_id), "kept, not deleted"
        assert _served(client, stable_id) == (404, 404)

        upsert_record(legacy, db_path=state_db_path)
        assert _served(client, stable_id) == (200, 200), "a fresh write is served again"
