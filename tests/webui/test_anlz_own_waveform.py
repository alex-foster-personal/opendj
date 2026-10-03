"""`/anlz` serves the OWN waveform record when that lane's source is own.

Acceptance lines:
  [if] a track decodes, waveform is own [then] /anlz serves tri bands from own, [else stop].
  [if] decode fails [then] status is failed/not_decoded, never a synth shape, [else stop].

PSSI phrase preservation when switching waveform to own is covered in
``tests/webui/test_anlz_pssi_on_own_switch.py``.

-Cursor
"""
from __future__ import annotations

import math
import struct
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from apps.adapters.rekordbox import config as rb_config
from apps.adapters.rekordbox.paths import empty_anlz_payload
from apps.analysis import selection
from apps.analysis.backends.own_waveform import OwnWaveformBackfillBackend, record_from_peaks
from apps.analysis.store import open_conn, upsert_record
from apps.webui.server.rb_vendor_pkg import anlz as anlz_mod
from apps.webui.server.rb_vendor_pkg import own_overlays
from apps.webui.server.rb_vendor_pkg import own_waveform_overlay as overlay_mod
from apps.webui.server.rb_vendor_pkg.own_waveform_overlay import OWN_WAVEFORM_MISSING_REASON

pytestmark = pytest.mark.requirement("NATIVE-06")

STABLE_ID = "b" * 40
SAMPLE_RATE_HZ = 44_100
TONE_HZ = 60.0
IN_BAND_MIN = 0.3
OUT_OF_BAND_MAX = 0.10


@pytest.fixture(autouse=True)
def _launch_state_toggles() -> Any:
    selection.reset_toggles()
    yield
    selection.reset_toggles()


@pytest.fixture
def state_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "state" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    open_conn(db_path).close()
    monkeypatch.setattr(rb_config, "STATE_DB", db_path)
    return db_path


def _write_sine_wav(path: Path, *, seconds: float) -> None:
    frames = bytearray()
    for i in range(int(SAMPLE_RATE_HZ * seconds)):
        value = int(32000 * math.sin(2 * math.pi * TONE_HZ * i / SAMPLE_RATE_HZ))
        frames += struct.pack("<h", value)
    import wave

    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE_HZ)
        handle.writeframes(bytes(frames))


def _rekordbox_waveform_stub() -> dict[str, Any]:
    return {
        "kind": "mono",
        "preview": {"length": 2, "low": [9, 9], "mid": [8, 8], "high": [7, 7]},
        "detail": {"length": 2, "low": [6, 6], "mid": [5, 5], "high": [4, 4]},
    }


def _payload_with_rekordbox_waveform(stable_id: str) -> dict[str, Any]:
    payload = empty_anlz_payload(stable_id, 400)
    payload["waveform"] = _rekordbox_waveform_stub()
    return payload


def _pssi_phrases() -> list[dict[str, Any]]:
    times = [round(index * 0.5, 3) for index in range(16)]
    entry_a = SimpleNamespace(beat=1, kind=1)
    entry_b = SimpleNamespace(beat=5, kind=2)
    content = SimpleNamespace(mood=2, end_beat=9, entries=[entry_a, entry_b])
    pssi = SimpleNamespace(content=content)
    phrases = anlz_mod._phrases_payload({"PSSI": pssi}, times)
    assert phrases
    return phrases


def test_unset_toggle_leaves_rekordbox_waveform_byte_identical(state_db: Path) -> None:
    import numpy as np

    peaks = np.full((300, 3), 128, dtype=np.uint8)
    record = record_from_peaks(
        STABLE_ID,
        peaks,
        duration_s=2.0,
        sample_rate=44100,
        decode_fingerprint="1f" * 32,
    )
    upsert_record(record, db_path=state_db)
    payload = _payload_with_rekordbox_waveform(STABLE_ID)
    before = payload["waveform"]

    served = overlay_mod.apply_own_waveform(payload, STABLE_ID, state_db)

    assert served["waveform"] == before


@pytest.mark.requires_ffmpeg
@pytest.mark.requires_canonical_decode
def test_own_ok_record_serves_tri_from_the_store(
    tmp_path: Path, state_db: Path
) -> None:
    audio = tmp_path / "tone.wav"
    _write_sine_wav(audio, seconds=2.0)
    record = OwnWaveformBackfillBackend.analyze(audio, STABLE_ID)
    upsert_record(record, db_path=state_db)
    selection.set_toggle("waveform", "own")
    payload = _payload_with_rekordbox_waveform(STABLE_ID)
    rekordbox_stub = payload["waveform"]

    served = overlay_mod.apply_own_waveform(payload, STABLE_ID, state_db)["waveform"]

    assert served["kind"] == "tri"
    assert served["status"] == "ok"
    assert served["preview"] == record.lanes["waveform"].payload["preview"]
    assert served["detail"] == record.lanes["waveform"].payload["detail"]
    assert served["preview"] != rekordbox_stub["preview"]
    low = served["detail"]["low"]
    mid = served["detail"]["mid"]
    high = served["detail"]["high"]
    assert max(low) > IN_BAND_MIN
    assert max(mid) <= OUT_OF_BAND_MAX
    assert max(high) <= OUT_OF_BAND_MAX


@pytest.mark.requires_ffmpeg
@pytest.mark.requires_canonical_decode
def test_own_failed_record_serves_not_decoded_without_synthesized_peaks(
    tmp_path: Path, state_db: Path
) -> None:
    source = tmp_path / "garbage.wav"
    source.write_bytes(b"this is not a RIFF header" * 4096)
    record = OwnWaveformBackfillBackend.analyze(source, STABLE_ID)
    upsert_record(record, db_path=state_db)
    selection.set_toggle("waveform", "own")
    phrases = _pssi_phrases()
    payload = _payload_with_rekordbox_waveform(STABLE_ID)
    payload["phrases"] = phrases

    served = own_overlays.apply_own_overlays(payload, STABLE_ID, state_db)

    assert served["waveform"]["status"] == "failed"
    assert "not_decoded" in served["waveform"]["reason"]
    assert served["waveform"]["preview"] == {"length": 0, "low": [], "mid": [], "high": []}
    assert served["waveform"]["detail"] == {"length": 0, "low": [], "mid": [], "high": []}
    assert served["phrases"] == phrases


def test_own_without_a_row_stays_missing(state_db: Path) -> None:
    selection.set_toggle("waveform", "own")
    payload = _payload_with_rekordbox_waveform(STABLE_ID)

    served = overlay_mod.apply_own_waveform(payload, STABLE_ID, state_db)["waveform"]

    assert served["status"] == "missing"
    assert served["reason"] == OWN_WAVEFORM_MISSING_REASON
    assert served["preview"] == {"length": 0, "low": [], "mid": [], "high": []}


@pytest.mark.requires_ffmpeg
@pytest.mark.requires_canonical_decode
def test_pssi_phrases_survive_own_ok_record_from_the_producer(
    tmp_path: Path, state_db: Path
) -> None:
    audio = tmp_path / "tone.wav"
    _write_sine_wav(audio, seconds=2.0)
    record = OwnWaveformBackfillBackend.analyze(audio, STABLE_ID)
    upsert_record(record, db_path=state_db)
    selection.set_toggle("waveform", "own")
    phrases = _pssi_phrases()
    payload = _payload_with_rekordbox_waveform(STABLE_ID)
    payload["phrases"] = phrases

    served = own_overlays.apply_own_overlays(payload, STABLE_ID, state_db)

    assert served["phrases"] == phrases
    assert served["waveform"]["kind"] == "tri"
    assert served["waveform"]["status"] == "ok"
