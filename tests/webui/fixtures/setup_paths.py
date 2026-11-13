"""Disposable on-disk folders for setup folder scan route tests (SETUP-22)."""

from __future__ import annotations

import struct
import unicodedata
import wave
from dataclasses import dataclass
from pathlib import Path


def write_playable_wav(path: Path, *, seconds: float = 0.1) -> Path:
    """Write a minimal wav the tag reader can open."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(44_100 * seconds)
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44_100)
        handle.writeframes(struct.pack("<" + "h" * frames, *([0] * frames)))
    return path


@dataclass(frozen=True)
class MissingFolderFixture:
    """A path the wizard might type that was never created."""

    path: Path


def missing_folder(root: Path, *, label: str = "Typo Crate That Was Never Made") -> MissingFolderFixture:
    return MissingFolderFixture(path=root / label)


@dataclass(frozen=True)
class MixedPlayabilityFixture:
    """One playable file beside a zero-byte wav wearing an audio extension."""

    root: Path
    valid_count: int
    junk_count: int


def mixed_zero_byte_and_valid(root: Path) -> MixedPlayabilityFixture:
    folder = root / "Mixed Bag"
    folder.mkdir(parents=True)
    write_playable_wav(folder / "valid.wav")
    (folder / "zero-bytes.wav").write_bytes(b"")
    return MixedPlayabilityFixture(root=folder, valid_count=1, junk_count=1)


@dataclass(frozen=True)
class UnicodeSpacesFixture:
    """NFD unicode with repeated spaces, matching the onboarding gauntlet."""

    root: Path
    track_prefix: str
    track_count: int


def unicode_repeated_spaces_folder(root: Path, *, track_count: int = 3) -> UnicodeSpacesFixture:
    folder_name = unicodedata.normalize("NFD", "Café  Crème Mix")
    prefix = unicodedata.normalize("NFD", "Señor Niño")
    folder = root / folder_name
    folder.mkdir(parents=True)
    for index in range(1, track_count + 1):
        write_playable_wav(folder / f"{prefix} {index:02d}.wav")
    return UnicodeSpacesFixture(root=folder, track_prefix=prefix, track_count=track_count)
