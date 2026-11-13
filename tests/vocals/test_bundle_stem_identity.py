"""bundle_stem_identity: stat tuple plus cheap content_fp per part."""

from __future__ import annotations

import json
import os
import threading
import wave
from pathlib import Path

import pytest

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


#: ``bundle_stem_identity`` of :func:`_golden_bundle`, computed on main at 24a2a564f5
#: (Fri 13 Nov 2026) before non-regular parts were special-cased; regular bundles must keep it.
GOLDEN_IDENTITY: str = "52f696807290c87a1753b1c39219d440dbd7aa6791450bf244539ad97ad15c47"


def _golden_bundle(root: Path) -> Path:
    """A bundle with fixed bytes and fixed mtimes per part, so its identity is a constant."""
    bundle_dir = _write_wav_bundle(root, "sid-golden-001")
    for index, part in enumerate(STEM_PARTS):
        path = bundle_dir / f"{part}.wav"
        path.write_bytes(path.read_bytes()[:-2] + bytes([index, 0]))
        mtime_ns = 1_700_000_000_000_000_000 + index * 1_000_000_000
        os.utime(path, ns=(mtime_ns, mtime_ns))
    return bundle_dir


@pytest.mark.requirement("STEM-13839")
def test_bundle_stem_identity_of_a_regular_bundle_matches_the_pre_change_golden(tmp_path: Path) -> None:
    """[if] every part is a regular file [then] the identity equals the value main computed before the FIFO guard, [else stop]."""
    _golden_bundle(tmp_path)
    assert vfrom_stems.bundle_stem_identity(load_stem_bundle("sid-golden-001", stems_dir=tmp_path)) == GOLDEN_IDENTITY


@pytest.mark.requirement("STEM-13839")
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs os.mkfifo (POSIX)")
def test_bundle_stem_identity_never_opens_a_fifo_part(tmp_path: Path) -> None:
    """[if] a part is a FIFO with no writer [then] the identity returns within 10 s and differs from the regular bundle, [else stop]."""
    bundle_dir = _golden_bundle(tmp_path)
    bundle = load_stem_bundle("sid-golden-001", stems_dir=tmp_path)
    part = bundle_dir / "other.wav"
    part.unlink()
    os.mkfifo(part)
    result: list[str] = []
    reader = threading.Thread(target=lambda: result.append(vfrom_stems.bundle_stem_identity(bundle)), daemon=True)
    reader.start()
    reader.join(timeout=10)
    if reader.is_alive():
        os.close(os.open(part, os.O_WRONLY | os.O_NONBLOCK))  # release the blocked open so the thread can exit
        pytest.fail("bundle_stem_identity blocked opening a FIFO part")
    assert result and result[0] != GOLDEN_IDENTITY


@pytest.mark.requirement("STEM-13839")
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs os.mkfifo (POSIX)")
def test_bundle_stem_identity_ignores_the_mtime_of_a_fifo_part(tmp_path: Path) -> None:
    """[if] only a FIFO part's mtime moves, as a write through it does [then] the identity is unchanged, [else stop]."""
    bundle_dir = _golden_bundle(tmp_path)
    bundle = load_stem_bundle("sid-golden-001", stems_dir=tmp_path)
    part = bundle_dir / "other.wav"
    part.unlink()
    os.mkfifo(part)
    before = vfrom_stems.bundle_stem_identity(bundle)
    moved_ns = part.stat().st_mtime_ns + 5_000_000_000
    os.utime(part, ns=(moved_ns, moved_ns))
    assert part.stat().st_mtime_ns == moved_ns
    assert vfrom_stems.bundle_stem_identity(bundle) == before
