"""`own_waveform.backfill`: registration, record shape, failure mapping, real audio.

The producer writes AnalysisRecord v2 rows that the mapped-track overlay
serves when waveform=own. Everything here uses real bytes and the real store;
ffmpeg-backed tests are marked ``requires_ffmpeg``.

[if] own_waveform.backfill writes a record [then] the store holds a valid v2 waveform, [else stop].

-Cursor
"""
from __future__ import annotations

import math
import struct
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from apps.analysis.backends import OWN_WAVEFORM_BACKEND, get_backend
from apps.analysis.backends.base import TrackVanished
from apps.analysis.backends.own_waveform import (
    BACKEND_NAME,
    OwnWaveformBackfillBackend,
    record_from_decode_error,
    record_from_peaks,
)
from apps.analysis.lanes import parse_own_backend
from apps.analysis.record import validate_record_contract
from apps.analysis.store import open_conn, upsert_record
from apps.analysis_waveform.lane_payload import build_waveform_payload
from apps.analysis_waveform.version import LANE, PRODUCER, PRODUCER_VERSION

pytestmark = pytest.mark.requirement("NATIVE-06")

FINGERPRINT_HEX = "1f" * 32
CANONICAL_FINGERPRINT = "sha256:" + FINGERPRINT_HEX
SAMPLE_RATE_HZ = 44_100
TONE_HZ = 60.0
IN_BAND_MIN = 0.3
OUT_OF_BAND_MAX = 0.10


#-----------------------------------------------------------------------------
# helpers
#-----------------------------------------------------------------------------

def _write_sine_wav(path: Path, *, seconds: float, hz: float = TONE_HZ) -> None:
    frames = bytearray()
    for i in range(int(SAMPLE_RATE_HZ * seconds)):
        value = int(32000 * math.sin(2 * math.pi * hz * i / SAMPLE_RATE_HZ))
        frames += struct.pack("<h", value)
    import wave

    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE_HZ)
        handle.writeframes(bytes(frames))


@pytest.fixture
def state_db(tmp_path: Path) -> Iterator[Path]:
    db_path = tmp_path / "state" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    open_conn(db_path).close()
    yield db_path


def _seed_library(db: Path, stable_id: str, minutes: float) -> None:
    audio = db.parent / f"{stable_id}.wav"
    audio.write_bytes(b"\0")
    conn = open_conn(db)
    conn.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, duration_ms, "
        "file_path, created_at, updated_at) VALUES (?, 'inferred', ?, ?, ?, "
        "'2026-09-09T00:00:00Z', '2026-09-09T00:00:00Z')",
        (stable_id, stable_id, int(minutes * 60_000), str(audio)),
    )
    conn.commit()
    conn.close()


#-----------------------------------------------------------------------------
# registration and naming
#-----------------------------------------------------------------------------

def test_the_registry_key_the_class_name_and_the_record_backend_are_one_string() -> None:
    assert OWN_WAVEFORM_BACKEND == "own_waveform.backfill"
    assert OWN_WAVEFORM_BACKEND == BACKEND_NAME
    assert get_backend(OWN_WAVEFORM_BACKEND) is OwnWaveformBackfillBackend
    assert OwnWaveformBackfillBackend.name == BACKEND_NAME
    assert OwnWaveformBackfillBackend.version == PRODUCER_VERSION


def test_the_backend_name_parses_back_to_this_lane_and_producer() -> None:
    parsed = parse_own_backend(BACKEND_NAME)
    assert parsed is not None
    assert (parsed.lane, parsed.producer, parsed.candidate) == (LANE, PRODUCER, None)


def test_the_shipping_producer_is_model_free() -> None:
    peaks = np.full((300, 3), 128, dtype=np.uint8)
    record = record_from_peaks(
        "sid",
        peaks,
        duration_s=2.0,
        sample_rate=44100,
        decode_fingerprint=FINGERPRINT_HEX,
    )
    assert record.uses_model is False
    assert record.model_sha256 is None
    assert record.decode_fingerprint == CANONICAL_FINGERPRINT
    validate_record_contract(record)


def test_jit_cache_is_absent() -> None:
    assert OwnWaveformBackfillBackend.jit_cache_roots() == ()
    assert "no jit cache" in OwnWaveformBackfillBackend.warm_jit_cache().lower()


def test_record_from_peaks_passes_the_store_contract(state_db: Path) -> None:
    peaks = np.zeros((450, 3), dtype=np.uint8)
    peaks[:, 0] = 200
    record = record_from_peaks(
        "sid",
        peaks,
        duration_s=3.0,
        sample_rate=44100,
        decode_fingerprint=FINGERPRINT_HEX,
    )
    lane = record.lanes["waveform"]
    assert lane.status == "ok"
    assert lane.payload["kind"] == "tri"
    assert lane.payload["preview"]["length"] > 0
    assert lane.payload["detail"]["length"] == 450
    validate_record_contract(record)
    upsert_record(record, db_path=state_db)


def test_record_from_decode_error_is_failed_with_empty_payload() -> None:
    record = record_from_decode_error("sid", "ffmpeg exited 1", FINGERPRINT_HEX)
    lane = record.lanes["waveform"]
    assert lane.status == "failed"
    assert lane.reason is not None
    assert "not_decoded" in lane.reason
    assert lane.payload == {}
    validate_record_contract(record)


def test_build_waveform_payload_never_duplicates_one_band_across_three() -> None:
    peaks = np.zeros((300, 3), dtype=np.uint8)
    peaks[:, 0] = 220
    payload = build_waveform_payload(peaks)
    low = payload["detail"]["low"]
    mid = payload["detail"]["mid"]
    high = payload["detail"]["high"]
    assert max(low) > IN_BAND_MIN
    assert max(mid) <= OUT_OF_BAND_MAX
    assert max(high) <= OUT_OF_BAND_MAX


#-----------------------------------------------------------------------------
# real audio, real decode, real store
#-----------------------------------------------------------------------------

@pytest.mark.requires_ffmpeg
@pytest.mark.requires_canonical_decode
def test_real_audio_produces_tri_bands(tmp_path: Path, state_db: Path) -> None:
    audio = tmp_path / "low-tone.wav"
    _write_sine_wav(audio, seconds=2.0)
    record = OwnWaveformBackfillBackend.analyze(audio, "sid-tone")
    lane = record.lanes["waveform"]
    assert lane.status == "ok", lane.reason
    assert lane.payload["kind"] == "tri"
    assert lane.payload["preview"]["length"] > 0
    assert lane.payload["detail"]["length"] > 0
    low = lane.payload["detail"]["low"]
    mid = lane.payload["detail"]["mid"]
    high = lane.payload["detail"]["high"]
    assert max(low) > IN_BAND_MIN
    assert max(mid) <= OUT_OF_BAND_MAX
    assert max(high) <= OUT_OF_BAND_MAX
    validate_record_contract(record)
    upsert_record(record, db_path=state_db)


@pytest.mark.requires_ffmpeg
@pytest.mark.requires_canonical_decode
def test_the_record_is_idempotent_across_two_runs(tmp_path: Path, state_db: Path) -> None:
    audio = tmp_path / "low-tone.wav"
    _write_sine_wav(audio, seconds=2.0)
    first = OwnWaveformBackfillBackend.analyze(audio, "sid-twice")
    upsert_record(first, db_path=state_db)
    second = OwnWaveformBackfillBackend.analyze(audio, "sid-twice")
    result = upsert_record(second, db_path=state_db)
    assert result.unchanged is True
    assert first.lanes["waveform"].payload == second.lanes["waveform"].payload


def test_a_vanished_path_raises_track_vanished(tmp_path: Path) -> None:
    missing = tmp_path / "gone.wav"
    with pytest.raises(TrackVanished):
        OwnWaveformBackfillBackend.analyze(missing, "sid-gone")


@pytest.mark.requires_ffmpeg
@pytest.mark.requires_canonical_decode
def test_a_garbage_file_writes_failed_not_decoded(tmp_path: Path, state_db: Path) -> None:
    source = tmp_path / "not really audio.wav"
    source.write_bytes(b"this is not a RIFF header" * 4096)
    record = OwnWaveformBackfillBackend.analyze(source, "sid-garbage")
    lane = record.lanes["waveform"]
    assert lane.status == "failed"
    assert lane.reason is not None
    assert "not_decoded" in lane.reason
    assert lane.payload == {}
    validate_record_contract(record)
    first = upsert_record(record, db_path=state_db)
    second = upsert_record(record, db_path=state_db)
    assert first.unchanged is False
    assert second.unchanged is True


def test_enqueue_resolves_the_waveform_backend(tmp_path: Path, monkeypatch) -> None:
    from apps.webui.server.app import create_app

    db_path = tmp_path / "state.db"
    monkeypatch.setenv("MDT_DATA_DIR", str(tmp_path))
    _seed_library(db_path, "wf-track", 4.0)
    app = create_app()
    app.state.analysis_db_path = db_path
    client = TestClient(app)
    resp = client.post(
        "/api/v1/analysis/backfill/enqueue",
        json={
            "stable_ids": ["wf-track"],
            "lane": "waveform",
            "backend": OWN_WAVEFORM_BACKEND,
        },
    )
    assert resp.status_code == 201, resp.text
    prog = client.get(
        "/api/v1/analysis/backfill/progress",
        params={"batch_id": resp.json()["batch_id"]},
    ).json()
    assert prog["items"][0]["stable_id"] == "wf-track"
    assert prog["items"][0]["state"] == "pending"
