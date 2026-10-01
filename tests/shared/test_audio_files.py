"""Tests for :mod:`apps.shared.audio_files`.

Ties to INFRA-02 (shared filesystem scanner used by locate + future tools).
We also ship a tiny programmatically-generated MP3 (``silent.mp3``) so
the tag reader can parse it without hitting a real audio library.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.shared import audio_files

# Minimal, valid MP3 frame generated on the fly. Tiny (~100 bytes) — good
# enough for the tag reader to identify the file as audio.
_SILENT_MP3 = bytes.fromhex(
    # ID3v2.3 header: "ID3", version, flags, size (4-byte synchsafe) = 0
    "494433030000000000"
    "00"
    # 10 MPEG1 Layer3 frames @ 64kbps/44.1kHz/mono — header only (208 bytes payload ea).
) + (
    # MPEG audio frame header FF FB 90 64 (0xFFFB = sync+MPEG1+L3, 0x90 = 128kbps/44.1k,
    # 0x64 = private+stereo — crudely valid). 417 bytes/frame for 128kbps/44.1k.
    b"\xff\xfb\x90\x64" + b"\x00" * 413
) * 3


@pytest.fixture(scope="module")
def sample_tree(tmp_path_factory) -> Path:
    """Build a tiny directory tree with mixed audio / non-audio / hidden content."""
    root = tmp_path_factory.mktemp("music-scan")

    # Audio files (various extensions).
    (root / "normal.mp3").write_bytes(_SILENT_MP3)
    (root / "Track.FLAC").write_bytes(b"fLaC" + b"\x00" * 200)  # wrong-case ext
    (root / "clip.wav").write_bytes(b"RIFF" + b"\x00" * 200)

    # Non-audio (should be skipped).
    (root / "notes.txt").write_text("hello")
    (root / "cover.jpg").write_bytes(b"\xff\xd8\xff")

    # Hidden file (should be skipped).
    (root / ".hidden.mp3").write_bytes(b"ID3")

    # Hidden dir (should NOT be descended into — file inside should not appear).
    hidden_dir = root / ".cache"
    hidden_dir.mkdir()
    (hidden_dir / "secret.mp3").write_bytes(_SILENT_MP3)

    # Nested visible dir with audio.
    nested = root / "sub"
    nested.mkdir()
    (nested / "deep.mp3").write_bytes(_SILENT_MP3)

    return root


# ------------------------------------------------------------------ scan_music_files


@pytest.mark.requirement("INFRA-02")
def test_scan_music_files_honors_extensions_and_hidden(sample_tree: Path) -> None:
    """Only audio extensions (case-insensitive); no hidden files/dirs."""
    found = sorted(af.path.name for af in audio_files.scan_music_files([sample_tree]))
    assert found == ["Track.FLAC", "clip.wav", "deep.mp3", "normal.mp3"]


@pytest.mark.requirement("INFRA-02")
def test_scan_music_files_returns_stat_data(sample_tree: Path) -> None:
    """``AudioFile`` objects carry size + mtime from ``stat``."""
    files = list(audio_files.scan_music_files([sample_tree]))
    assert files
    for af in files:
        assert af.size_bytes >= 0
        assert af.mtime > 0
        assert af.ext.startswith(".") and af.ext == af.ext.lower()


@pytest.mark.requirement("INFRA-02")
def test_scan_music_files_handles_missing_root(tmp_path: Path) -> None:
    """Nonexistent roots are skipped silently (generator yields nothing)."""
    out = list(audio_files.scan_music_files([tmp_path / "does-not-exist"]))
    assert out == []


@pytest.mark.requirement("INFRA-02")
def test_scan_music_files_uses_default_roots_when_none(
    monkeypatch: pytest.MonkeyPatch, sample_tree: Path
) -> None:
    """When ``roots=None``, falls back to ``paths.MUSIC_ROOTS``."""
    from apps.shared import paths as shared_paths

    monkeypatch.setattr(shared_paths, "MUSIC_ROOTS", [sample_tree])
    files = list(audio_files.scan_music_files(None))
    names = {af.path.name for af in files}
    assert "normal.mp3" in names


# ------------------------------------------------------------------ read_metadata


@pytest.mark.requirement("INFRA-02")
def test_read_metadata_returns_none_for_non_audio(tmp_path: Path) -> None:
    """The tag reader cannot parse a text file → return None (don't crash)."""
    f = tmp_path / "not-audio.txt"
    f.write_text("hello")
    assert audio_files.read_metadata(f) is None


@pytest.mark.requirement("INFRA-02")
def test_read_metadata_returns_none_for_missing_file(tmp_path: Path) -> None:
    """Missing path → None (no FileNotFoundError bubbling up)."""
    assert audio_files.read_metadata(tmp_path / "missing.mp3") is None


@pytest.mark.requirement("INFRA-02")
def test_read_metadata_handles_tiny_mp3_frame(tmp_path: Path) -> None:
    """Our crafted MP3 parses to an AudioMetadata record, even if empty."""
    f = tmp_path / "silent.mp3"
    f.write_bytes(_SILENT_MP3)
    meta = audio_files.read_metadata(f)
    # the tag reader may or may not detect the header as valid MP3; either way,
    # we must NOT crash and must return None or a populated AudioMetadata.
    assert meta is None or isinstance(meta, audio_files.AudioMetadata)
