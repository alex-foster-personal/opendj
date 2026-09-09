"""The share-audience decode must read the file the share listener actually hears.

Split out of ``test_local_waveform_decode.py`` (issue #735 follow-up) purely to keep
that file under the repo's 600-line file-size gate; no behavior moved.

Regression one-liner:
  - if a Share listener's peaks come from the wrong (non-Warehouse-windowed) audio file then broken
"""
from __future__ import annotations

import math
import shutil
import struct
import subprocess
import wave
from pathlib import Path

import pytest

from apps.adapters.rekordbox import config as rb_config
from apps.analysis_waveform import local_waveform
from apps.shared.state import db as state_db
from apps.shared.state.writer import StateWriter

pytestmark = [pytest.mark.requirement("PARITY-03"), pytest.mark.rb_parity]

LOCAL_SID = "e" * 40
SAMPLE_RATE_HZ = 44_100
LOUD_S = 2.0
SILENT_S = 2.0
DURATION_MS = int((LOUD_S + SILENT_S) * 1000)


def _write_wav(path: Path, *, loud_s: float, silent_s: float) -> None:
    """A real WAV: ``loud_s`` of a full-scale 220 Hz sine, then digital silence."""
    frames = bytearray()
    for i in range(int(SAMPLE_RATE_HZ * loud_s)):
        value = int(32000 * math.sin(2 * math.pi * 220.0 * i / SAMPLE_RATE_HZ))
        frames += struct.pack("<h", value)
    frames += b"\x00\x00" * int(SAMPLE_RATE_HZ * silent_s)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE_HZ)
        handle.writeframes(bytes(frames))


def _encode_low_bitrate_mp3(wav_path: Path, mp3_path: Path) -> None:
    """A REAL ffmpeg encode -- decodable frames, not fabricated bytes -- ranked
    below a .wav master by apps.shared.audio_quality purely on container + bitrate."""
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None, "the requires_ffmpeg marker guarantees ffmpeg is on PATH"
    subprocess.run(
        [
            ffmpeg, "-y", "-i", str(wav_path),
            "-b:a", "96k", str(mp3_path),
        ],
        check=True, capture_output=True, timeout=30,
    )


@pytest.mark.requires_ffmpeg
def test_share_audience_decodes_the_file_that_audience_actually_hears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two REAL candidate files, differently ranked (.wav master vs 96kbps .mp3
    alternate): local windows to Stadium, share to Warehouse, so the two audiences
    must decode DIFFERENT bytes, proving share reaches the real resolver."""
    state_path = tmp_path / "state.db"
    monkeypatch.setattr(rb_config, "STATE_DB", state_path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", tmp_path / "absent.db")
    monkeypatch.setattr(rb_config, "LOCAL_WAVEFORM_CACHE_DIR", tmp_path / "cache")

    master = tmp_path / "master.wav"
    _write_wav(master, loud_s=2.0, silent_s=2.0)
    radio_source = tmp_path / "radio-source.wav"
    _write_wav(radio_source, loud_s=0.0, silent_s=4.0)
    radio_mp3 = tmp_path / "radio.mp3"
    _encode_low_bitrate_mp3(radio_source, radio_mp3)

    conn = state_db.open_rw(state_path)
    try:
        writer = StateWriter(conn, actor="unit-test")
        writer.upsert_track(
            stable_id=LOCAL_SID,
            stable_id_tier="inferred",
            title="Demo",
            artists=["X"],
            album=None,
            isrc=None,
            duration_ms=DURATION_MS,
            file_path=str(master),
        )
        writer.upsert_track_location(
            stable_id=LOCAL_SID, kind="local", file_path=str(radio_mp3),
        )
        writer.close()
    finally:
        conn.close()

    local_peaks = local_waveform.ensure_local_peaks(LOCAL_SID, share=False)
    share_peaks = local_waveform.ensure_local_peaks(LOCAL_SID, share=True)
    assert int(local_peaks.max()) > 0, "local audience must decode the loud master"
    assert int(share_peaks.max()) == 0, (
        "share audience must decode the windowed-down alternate, not the master"
    )
