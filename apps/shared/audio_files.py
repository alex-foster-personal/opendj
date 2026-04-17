"""Filesystem audio scanner + lightweight metadata reader (mutagen)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from . import paths
from ._mutagen import HAS_MUTAGEN


@dataclass(slots=True)
class AudioFile:
    path: Path
    size_bytes: int
    mtime: float
    ext: str  # lowercased, includes leading dot


@dataclass(slots=True)
class AudioMetadata:
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    duration_s: float | None = None
    bitrate_kbps: int | None = None
    sample_rate: int | None = None


def scan_music_files(roots: list[Path] | None = None) -> Iterator[AudioFile]:
    """Walk ``roots`` (default :data:`paths.MUSIC_ROOTS`) yielding audio files.

    Hidden directories (name starts with ``.``) are skipped. Extensions are
    compared case-insensitively against :data:`paths.AUDIO_EXTENSIONS`.
    """
    use_roots = roots if roots is not None else paths.MUSIC_ROOTS
    for root in use_roots:
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            # Prune hidden dirs in-place so os.walk doesn't descend into them.
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                if name.startswith("."):
                    continue
                ext = os.path.splitext(name)[1].lower()
                if ext not in paths.AUDIO_EXTENSIONS:
                    continue
                full = Path(dirpath) / name
                try:
                    st = full.stat()
                except (OSError, FileNotFoundError):
                    continue
                yield AudioFile(path=full, size_bytes=st.st_size, mtime=st.st_mtime, ext=ext)


def _first(tags, key: str) -> str | None:
    val = tags.get(key) if tags else None
    if not val:
        return None
    # mutagen easy-mode returns lists of strings.
    if isinstance(val, list):
        val = val[0] if val else None
    if val is None:
        return None
    s = str(val).strip()
    return s or None


def read_metadata(path: Path) -> AudioMetadata | None:
    """Read audio metadata via mutagen's easy interface. ``None`` on failure.

    When the optional ``mutagen`` dep (``music-dj-tools[tags]``) is not
    installed this is a best-effort no-op that returns ``None``; the scanner
    layer still yields :class:`AudioFile` entries from the filesystem.
    """
    if not HAS_MUTAGEN:
        return None
    import mutagen  # type: ignore  # guarded above

    try:
        f = mutagen.File(str(path), easy=True)
    except Exception:
        return None
    if f is None:
        return None

    info = getattr(f, "info", None)
    bitrate = getattr(info, "bitrate", None) if info is not None else None
    bitrate_kbps = int(bitrate / 1000) if bitrate else None

    return AudioMetadata(
        title=_first(f, "title"),
        artist=_first(f, "artist"),
        album=_first(f, "album"),
        duration_s=float(info.length) if info and getattr(info, "length", None) else None,
        bitrate_kbps=bitrate_kbps,
        sample_rate=int(info.sample_rate) if info and getattr(info, "sample_rate", None) else None,
    )


__all__ = [
    "AudioFile",
    "AudioMetadata",
    "scan_music_files",
    "read_metadata",
]
