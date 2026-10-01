"""from-stems: derive vocal-cache from existing stem bundles (CPU, no demucs)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

sf = pytest.importorskip("soundfile", reason="needs the optional soundfile package")

from apps.stems.artifacts import STEM_PARTS, load_stem_bundle  # noqa: E402
from apps.vocals import cache as vcache  # noqa: E402
from apps.vocals import from_stems as vfrom_stems  # noqa: E402

pytestmark = [
    pytest.mark.requirement("CAT-05"),
    pytest.mark.requires_audio_stack,
]


def _write_v1_bundle(root: Path, stable_id: str, *, sr: int = 44100) -> Path:
    """Tiny aligned WAV bundle: vocals loud mid-track, accompaniment quiet."""
    bundle = root / stable_id
    bundle.mkdir(parents=True)
    n = sr * 4  # 4 s
    t = np.arange(n, dtype=np.float32) / sr
    vocals = np.zeros(n, dtype=np.float32)
    vocals[sr : 3 * sr] = 0.4 * np.sin(2 * np.pi * 440 * t[sr : 3 * sr])
    quiet = 0.02 * np.sin(2 * np.pi * 110 * t)
    for part, mono in (
        ("vocals", vocals),
        ("drums", quiet),
        ("bass", quiet),
        ("other", quiet),
    ):
        sf.write(str(bundle / f"{part}.wav"), mono, sr, subtype="PCM_16")
    manifest = {
        "schema_version": 1,
        "stable_id": stable_id,
        "model": {"name": "htdemucs", "version": "4.0.1"},
        "source": {
            "path": "/tmp/source.wav",
            "sha256": "a" * 64,
        },
        "files": {p: f"{p}.wav" for p in STEM_PARTS},
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return bundle


def test_derive_worker_result_finds_mid_track_vocals(tmp_path: Path) -> None:
    sid = "abc123fromstems000000000000000000000001"
    _write_v1_bundle(tmp_path, sid)
    bundle = load_stem_bundle(sid, stems_dir=tmp_path)
    result = vfrom_stems.derive_worker_result(bundle)
    assert result["source"] == vcache.VOCAL_CACHE_SOURCE
    assert result["schema"] == vcache.VOCAL_CACHE_SCHEMA
    assert result["params"]["derived_from_stems"] is True
    assert result["device"] == "from-stems"
    assert result["regions"], "expected at least one vocal region"
    # Mid-track sine should land near [1s, 3s] after hysteresis/merge.
    starts = [r["start_s"] for r in result["regions"]]
    ends = [r["end_s"] for r in result["regions"]]
    assert min(starts) <= 1.5
    assert max(ends) >= 2.5


def test_write_from_bundle_publishes_valid_cache(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    stems = data_dir / "state" / "stems"
    sid = "abc123fromstems000000000000000000000002"
    _write_v1_bundle(stems, sid)
    audio = tmp_path / "source.wav"
    # Source identity for cache invalidation; content need not match stems.
    sf.write(str(audio), np.zeros(44100, dtype=np.float32), 44100, subtype="PCM_16")
    entry = vfrom_stems.write_from_bundle(data_dir, sid, audio)
    loaded = vcache.load_valid_entry(vcache.cache_path(data_dir, sid), audio)
    assert loaded is not None
    assert loaded["coverage_pct"] == entry["coverage_pct"]
    assert loaded["regions"]
