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
        "schema": vcache.VOCAL_CACHE_SCHEMA,
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
    assert loaded["schema"] == vcache.VOCAL_CACHE_SCHEMA
    assert loaded["source"] == "demucs-htdemucs"
    assert loaded["audio_mtime"] == audio.stat().st_mtime
    assert loaded["confidence"] == 0.84  # scalar = max region confidence
    assert loaded["regions"][0]["intensity"] == 3  # round(0.84 * 4)
    assert loaded["regions"][1]["intensity"] == 1  # floor at 1


def test_stem_upload_mapping_is_persisted_when_present(
    tmp_path: Path, audio: Path
) -> None:
    # The R2 destination's returned content-addressed keys are the only link
    # from this stable_id to its opaque objects; they must survive the cache
    # write or the bundle cannot be rediscovered after the run exits.
    path = vcache.cache_path(tmp_path, "abc123")
    upload = {
        "manifest_key": "assets/ab/" + "ab" * 32,
        "keys": ["assets/cd/" + "cd" * 32, "assets/ef/" + "ef" * 32],
        "bytes": 123456,
    }
    written = vcache.write_entry(path, _worker_result(stem_upload=upload), audio)
    assert written["worker"]["stem_upload"] == upload


def test_stem_upload_is_absent_for_non_r2_destinations(
    tmp_path: Path, audio: Path
) -> None:
    # local/none runs never produce stem_upload; the entry must not carry an
    # empty placeholder that a reader could mistake for a published bundle.
    path = vcache.cache_path(tmp_path, "abc123")
    written = vcache.write_entry(path, _worker_result(), audio)
    assert "stem_upload" not in written["worker"]


def test_non_r2_refarm_preserves_an_existing_r2_stem_upload_mapping(
    tmp_path: Path,
) -> None:
    """A refarm without an upload must not orphan prior opaque R2 objects."""
    path = vcache.cache_path(tmp_path, "stable-id")
    audio = tmp_path / "track.wav"
    audio.write_bytes(b"audio")
    upload = {"part_keys": ["assets/sha256/part"], "manifest_key": "assets/sha256/manifest"}
    vcache.write_entry(path, _worker_result(stem_upload=upload), audio)

    refarmed = vcache.write_entry(path, _worker_result(), audio)

    assert refarmed["worker"]["stem_upload"] == upload
    persisted = vcache.load_valid_entry(path, audio)
    assert persisted is not None
    assert persisted["worker"]["stem_upload"] == upload


def test_non_r2_refarm_does_not_preserve_r2_mapping_for_replaced_audio(
    tmp_path: Path,
) -> None:
    """A new audio generation must not inherit an old R2 stem bundle."""
    path = vcache.cache_path(tmp_path, "stable-id")
    audio = tmp_path / "track.wav"
    audio.write_bytes(b"old audio")
    upload = {"part_keys": ["assets/sha256/part"], "manifest_key": "assets/sha256/manifest"}
    vcache.write_entry(path, _worker_result(stem_upload=upload), audio)
    audio.write_bytes(b"replacement audio with a different size")

    refarmed = vcache.write_entry(path, _worker_result(), audio)

    assert "stem_upload" not in refarmed["worker"]


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
    path.write_text(json.dumps({"schema": vcache.VOCAL_CACHE_SCHEMA}), encoding="utf-8")
    with pytest.raises(ValueError, match="missing fields"):
        vcache.load_valid_entry(path, audio)


def test_schema_bump_self_heals_as_none(tmp_path: Path, audio: Path) -> None:
    path = vcache.cache_path(tmp_path, "abc123")
    entry = vcache.write_entry(path, _worker_result(), audio)
    entry["schema"] = 999
    path.write_text(json.dumps(entry), encoding="utf-8")
    assert vcache.load_valid_entry(path, audio) is None


def test_source_replacement_with_preserved_mtime_invalidates(
    tmp_path: Path, audio: Path
) -> None:
    """[if] source is swapped but keeps its timestamp [then] cache misses."""
    path = vcache.cache_path(tmp_path, "abc123")
    vcache.write_entry(path, _worker_result(), audio)
    original_mtime_ns = audio.stat().st_mtime_ns
    replacement = tmp_path / "replacement.mp3"
    replacement.write_bytes(b"different audio bytes")
    os.utime(replacement, ns=(replacement.stat().st_atime_ns, original_mtime_ns))
    os.replace(replacement, audio)
    assert audio.stat().st_mtime_ns == original_mtime_ns
    assert vcache.load_valid_entry(path, audio) is None


def test_malformed_region_raises_instead_of_serving(tmp_path: Path, audio: Path) -> None:
    """[if] cached region has an invalid confidence [then] fail loudly."""
    path = vcache.cache_path(tmp_path, "abc123")
    entry = vcache.write_entry(path, _worker_result(), audio)
    entry["regions"][0]["confidence"] = 1.5
    path.write_text(json.dumps(entry), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid region"):
        vcache.load_valid_entry(path, audio)


def test_cache_path_rejects_directory_escape(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="escapes"):
        vcache.cache_path(tmp_path, "../outside")


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
    "overrides",
    [
        {"fps": 0.0},
        {"fps": float("nan")},
        {"duration_s": 0.0},
        {"coverage_pct": 100.1},
        {
            "regions": [
                {"start_s": 10.0, "end_s": 121.0, "confidence": 0.84},
            ],
        },
    ],
)
def test_write_semantic_validation_never_publishes_poison_entry(
    tmp_path: Path, audio: Path, overrides: dict[str, Any],
) -> None:
    path = vcache.cache_path(tmp_path, "poison")
    with pytest.raises(ValueError):
        vcache.write_entry(path, _worker_result(**overrides), audio)
    assert not path.exists()
    assert not list(path.parent.glob(f".{path.name}.*.tmp"))


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


# ----- preset stamping (Modal farm, scripts/modal_vocal_farm.py) --------------
#   - if a preset-stamped entry loses its preset on reload then broken
#   - if omitting preset invents one then broken (unstamped must stay visible)
#   - if an empty/non-mapping preset is accepted then broken

_PRESET: dict[str, Any] = {
    "tag": "htdemucs-ov0.1", "model": "htdemucs",
    "overlap": 0.1, "shifts": 0, "rung": 3,
}


def test_preset_stamp_roundtrips(tmp_path: Path, audio: Path) -> None:
    path = vcache.cache_path(tmp_path, "stamped")
    written = vcache.write_entry(path, _worker_result(), audio, preset=_PRESET)
    assert written["preset"] == _PRESET
    loaded = vcache.load_valid_entry(path, audio)
    assert loaded is not None
    assert loaded["preset"] == _PRESET


def test_preset_omitted_leaves_key_absent(tmp_path: Path, audio: Path) -> None:
    """Unstamped must stay distinguishable from stamped-as-default, or a
    selective re-run cannot tell which entries predate the stamp."""
    path = vcache.cache_path(tmp_path, "unstamped")
    written = vcache.write_entry(path, _worker_result(), audio)
    assert "preset" not in written
    loaded = vcache.load_valid_entry(path, audio)
    assert loaded is not None and "preset" not in loaded


@pytest.mark.parametrize("bad", [{}, "htdemucs-ov0.1", 3])
def test_preset_rejects_non_mapping(tmp_path: Path, audio: Path, bad: Any) -> None:
    path = vcache.cache_path(tmp_path, "badpreset")
    with pytest.raises(ValueError, match="preset must be a non-empty mapping"):
        vcache.write_entry(path, _worker_result(), audio, preset=bad)
    assert not path.exists()
