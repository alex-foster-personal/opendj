"""apps.vocals.cache contract tests (SPIKE-SUMMARY section 3).

Regression one-liners:
  - if a vocal-cache entry lacks source/schema fields then broken (loudly)
  - if an entry survives an audio_mtime change then broken
  - if intensity_of doesn't map confidence 0..1 onto the 1..4 PVDI ramp then broken
  - if anlz_vocals_of doesn't emit status 'demucs' with PVDI-shaped regions then broken
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from apps.vocals import cache as vcache

pytestmark = pytest.mark.requirement("CAT-05")


def _worker_result(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema": 1,
        "source": "demucs-htdemucs",
        "fps": 2.0,
        "duration_s": 120.0,
        "coverage_pct": 41.7,
        "regions": [
            {"start_s": 10.0, "end_s": 60.0, "confidence": 0.84},
            {"start_s": 80.0, "end_s": 90.0, "confidence": 0.2},
        ],
        "params": {"hop_s": 0.5, "on_ratio": 0.1},
        "device": "cpu",
        "timings": {"load_s": 1.0, "separate_s": 100.0},
        "source_sample_rate": 44100,
        "analysis_sample_rate": 44100,
    }
    base.update(overrides)
    return base


@pytest.fixture
def audio(tmp_path: Path) -> Path:
    f = tmp_path / "track.mp3"
    f.write_bytes(b"not really audio")
    return f


def test_write_then_load_roundtrip(tmp_path: Path, audio: Path) -> None:
    path = vcache.cache_path(tmp_path, "abc123")
    written = vcache.write_entry(path, _worker_result(), audio)
    loaded = vcache.load_valid_entry(path, audio)
    assert loaded == written
    assert loaded is not None
    assert loaded["schema"] == 1
    assert loaded["source"] == "demucs-htdemucs"
    assert loaded["audio_mtime"] == audio.stat().st_mtime
    assert loaded["confidence"] == 0.84  # scalar = max region confidence
    assert loaded["regions"][0]["intensity"] == 3  # round(0.84 * 4)
    assert loaded["regions"][1]["intensity"] == 1  # floor at 1


def test_audio_mtime_change_invalidates(tmp_path: Path, audio: Path) -> None:
    path = vcache.cache_path(tmp_path, "abc123")
    vcache.write_entry(path, _worker_result(), audio)
    os.utime(audio, (audio.stat().st_atime, audio.stat().st_mtime + 10))
    assert vcache.load_valid_entry(path, audio) is None


def test_missing_audio_never_serves(tmp_path: Path, audio: Path) -> None:
    path = vcache.cache_path(tmp_path, "abc123")
    vcache.write_entry(path, _worker_result(), audio)
    audio.unlink()
    assert vcache.load_valid_entry(path, audio) is None
    assert vcache.load_valid_entry(path, None) is None


def test_absent_entry_is_none_but_corrupt_raises(tmp_path: Path, audio: Path) -> None:
    path = vcache.cache_path(tmp_path, "missing")
    assert vcache.load_valid_entry(path, audio) is None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        vcache.load_valid_entry(path, audio)


def test_entry_missing_contract_fields_raises(tmp_path: Path, audio: Path) -> None:
    path = vcache.cache_path(tmp_path, "abc123")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": 1}), encoding="utf-8")
    with pytest.raises(ValueError, match="missing fields"):
        vcache.load_valid_entry(path, audio)


def test_schema_bump_self_heals_as_none(tmp_path: Path, audio: Path) -> None:
    path = vcache.cache_path(tmp_path, "abc123")
    entry = vcache.write_entry(path, _worker_result(), audio)
    entry["schema"] = 999
    path.write_text(json.dumps(entry), encoding="utf-8")
    assert vcache.load_valid_entry(path, audio) is None


def test_write_rejects_incomplete_worker_result(tmp_path: Path, audio: Path) -> None:
    bad = _worker_result()
    del bad["regions"]
    with pytest.raises(ValueError, match="missing fields"):
        vcache.write_entry(vcache.cache_path(tmp_path, "x"), bad, audio)


def test_write_rejects_wrong_source(tmp_path: Path, audio: Path) -> None:
    with pytest.raises(ValueError, match="source"):
        vcache.write_entry(
            vcache.cache_path(tmp_path, "x"),
            _worker_result(source="silero-vad"),  # rejected candidate (B2)
            audio,
        )


@pytest.mark.parametrize(
    ("confidence", "intensity"),
    [(0.0, 1), (0.2, 1), (0.5, 2), (0.84, 3), (1.0, 4)],
)
def test_intensity_ramp(confidence: float, intensity: int) -> None:
    assert vcache.intensity_of(confidence) == intensity


def test_intensity_rejects_out_of_range() -> None:
    with pytest.raises(ValueError, match="out of range"):
        vcache.intensity_of(1.2)


def test_anlz_vocals_of_shape(tmp_path: Path, audio: Path) -> None:
    path = vcache.cache_path(tmp_path, "abc123")
    entry = vcache.write_entry(path, _worker_result(), audio)
    vocals = vcache.anlz_vocals_of(entry)
    assert vocals["status"] == "demucs"
    assert vocals["fps"] == 2.0
    assert vocals["regions"][0] == {
        "start_s": 10.0, "end_s": 60.0, "intensity": 3, "confidence": 0.84,
    }
