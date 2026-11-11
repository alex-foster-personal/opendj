"""bundle_stem_identity: stat tuple plus cheap content_fp per part."""

from __future__ import annotations

import json
import os
import wave
from pathlib import Path

from apps.stems.artifacts import STEM_PARTS, load_stem_bundle
from apps.vocals import from_stems as vfrom_stems


def _write_wav_bundle(root: Path, stable_id: str, *, duration_s: float = 1.0, sr: int = 8000) -> Path:
    bundle_dir = root / stable_id
    bundle_dir.mkdir(parents=True)
    n_frames = int(duration_s * sr)
    for part in STEM_PARTS:
        with wave.open(str(bundle_dir / f"{part}.wav"), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sr)
            wav_file.writeframes(b"\x00\x00" * n_frames)
    manifest = {
        "schema_version": 1,
        "stable_id": stable_id,
        "model": {"name": "htdemucs", "version": "4.0.1"},
        "source": {"path": f"/music/{stable_id}.flac", "sha256": "a" * 64},
        "files": {part: f"{part}.wav" for part in STEM_PARTS},
    }
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return bundle_dir


def _rewrite_vocals_same_size(bundle_dir: Path, *, duration_s: float, sr: int) -> None:
    n_frames = int(duration_s * sr)
    path = bundle_dir / "vocals.wav"
    mtime_before_ns = path.stat().st_mtime_ns
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sr)
        wav_file.writeframes(b"\x01\x00" * n_frames)
    os.utime(path, ns=(mtime_before_ns, mtime_before_ns))
    assert path.stat().st_mtime_ns == mtime_before_ns


def test_bundle_stem_identity_moves_when_mtime_and_size_are_unchanged(tmp_path: Path) -> None:
    """[if] a same-size part rewrite keeps mtime_ns fixed [then] bundle_stem_identity differs, [else stop]."""
    bundle_dir = _write_wav_bundle(tmp_path, "sid-content-fp-001")
    before = vfrom_stems.bundle_stem_identity(load_stem_bundle("sid-content-fp-001", stems_dir=tmp_path))
    _rewrite_vocals_same_size(bundle_dir, duration_s=1.0, sr=8000)
    after = vfrom_stems.bundle_stem_identity(load_stem_bundle("sid-content-fp-001", stems_dir=tmp_path))
    assert before != after
