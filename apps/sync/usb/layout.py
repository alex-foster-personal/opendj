"""Destination-path computation for USB mirror layouts.

Keeps the three D3 layouts isolated from the diff engine so callers can
unit-test path generation independently.
"""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

# Characters forbidden on FAT/exFAT; we substitute underscore. Forward
# slash is handled specially (it is a path separator).
_FORBIDDEN_RE = re.compile(r'[\\:*?"<>|]')


def sanitise_segment(segment: str) -> str:
    """Return ``segment`` with filesystem-hostile chars replaced by ``_``."""
    cleaned = _FORBIDDEN_RE.sub("_", segment)
    # Also replace slashes, which are path separators not valid in a name.
    cleaned = cleaned.replace("/", "_")
    # Strip trailing dots/spaces -- FAT32 rejects them.
    cleaned = cleaned.rstrip(". ")
    return cleaned or "_"


def extension_for(format_: str, src: Path) -> str:
    """Return extension (with leading dot) for the destination file."""
    if format_ == "mp3@320":
        return ".mp3"
    return src.suffix.lower()


def dst_relpath(
    *,
    layout: str,
    format_: str,
    playlist: str,
    track_index: int,
    artist: str,
    album: str,
    title: str,
    src: Path,
) -> PurePosixPath:
    """Compute drive-relative destination path (forward slashes).

    The rules match ``10-CONTEXT.md#D3`` and ``10-RESEARCH.md#5``.
    Sanitisation is applied per segment; we refuse empty segments by
    defaulting to the literal ``"_"``.
    """
    ext = extension_for(format_, src)
    title_clean = sanitise_segment(title or src.stem)
    artist_clean = sanitise_segment(artist or "Unknown Artist")

    if layout == "Artist/Album/Track":
        album_clean = sanitise_segment(album or "Unknown Album")
        return PurePosixPath(artist_clean, album_clean, title_clean + ext)

    if layout == "Playlist/NN - Artist - Track":
        playlist_clean = sanitise_segment(playlist or "Playlist")
        nn = f"{track_index:02d}"
        stem = f"{nn} - {artist_clean} - {title_clean}"
        return PurePosixPath(playlist_clean, sanitise_segment(stem) + ext)

    if layout == "flat":
        stem = f"{artist_clean} - {title_clean}"
        return PurePosixPath(sanitise_segment(stem) + ext)

    raise ValueError(f"unknown layout: {layout!r}")


__all__ = ["sanitise_segment", "extension_for", "dst_relpath"]
