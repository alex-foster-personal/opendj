"""Single import-guard and entry point for the audio tag reader (tinytag).

Why this exists
---------------

Tag reading used to go through ``mutagen``, which is GPL-2.0-or-later and so
could only ship as an opt-in extra of this Apache-2.0 package; the desktop
payload omitted it and every tag-backed surface (embedded artwork, file
genre, upload duration) reported "reader unavailable". Since Thu 1 Oct 2026
every READ goes through ``tinytag`` (MIT, pure Python), a core dependency.
Research: ``research/tag-reading/2026-10-01-mutagen-replacement.md``.

mutagen remains only behind the opt-in ``[tags]`` extra for the tag WRITE
family (``apps.shared.tag_writer``, ``apps.analysis.write_tags``, the Serato
GEOB codec), guarded by :mod:`apps.shared._mutagen`. Nothing that reads tags
for the library, ingest, artwork or reconcile surfaces may import mutagen.

Every callsite imports from this module, so:

* Modules still import when ``tinytag`` is absent (a broken or hand-built
  environment); :data:`HAS_TAG_READER` is ``False`` and the HTTP surfaces
  answer their documented "reader unavailable" 503 rather than guessing.
* A caller that genuinely needs tags invokes :func:`require` and gets a
  clean, self-describing :class:`ImportError`.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

try:  # pragma: no cover - trivially exercised at import time
    import tinytag as _tinytag  # type: ignore
    HAS_TAG_READER: bool = True
except ImportError:  # pragma: no cover - only on environments missing the core dep
    _tinytag = None  # type: ignore[assignment]
    HAS_TAG_READER = False

if TYPE_CHECKING:  # pragma: no cover
    from tinytag import TinyTag


_INSTALL_HINT = (
    "audio tag reading requires the 'tinytag' package, a core dependency of "
    "music-dj-tools; reinstall the environment (uv sync)."
)


def require() -> None:
    """Raise :class:`ImportError` with an install hint when tinytag is absent."""
    if not HAS_TAG_READER:
        raise ImportError(_INSTALL_HINT)


def can_read(path: Path | str) -> bool:
    """Whether tinytag parses this file type at all.

    tinytag has no reader for some formats the library accepts (raw ADTS
    ``.aac``, measured on tinytag 2.3.2), and returns no duration for them
    rather than raising. A caller that treats a missing duration as damage
    must ask this first, or every such file reads as broken.
    """
    if _tinytag is None:
        return False
    from tinytag import TinyTag  # type: ignore

    return Path(path).suffix.lower() in TinyTag.SUPPORTED_FILE_EXTENSIONS


class TagReadError(Exception):
    """The file could not be parsed as tagged audio (corrupt, truncated, not audio)."""


def read(path: Path | str, *, image: bool = False, duration: bool = True) -> TinyTag:
    """Parse ``path`` with tinytag; raise :class:`TagReadError` when it cannot.

    Callers decide whether a failure is data (a library walk) or an error
    (an upload probe). ``image=True`` also loads embedded pictures.
    ``duration=False`` skips the stream scan for callers that only want tags
    or pictures: the listing's artwork probe runs per row, and the scan is
    the larger share of a parse (measured 42 vs 28 us on a 3 s mp3, more on
    long VBR files).
    """
    require()
    from tinytag import TinyTag  # type: ignore

    try:
        tag = TinyTag.get(str(path), tags=True, duration=duration, image=image)
    except Exception as exc:
        raise TagReadError(str(exc) or type(exc).__name__) from exc
    if duration:
        # tinytag misreads raw ADTS streams (0.03 s for a 7.3 s file) rather
        # than failing, so every duration consumer takes the frame walk.
        adts = adts_duration(path)
        if adts is not None:
            tag.duration = adts
    return tag


_ADTS_RATES = (
    96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000,
    11025, 8000, 7350,
)


def adts_duration(path: Path | str) -> float | None:
    """Duration in seconds of a raw ADTS AAC stream, or None if it is not one.

    tinytag has no ADTS reader (see :func:`can_read`), and the upload duplicate
    check needs a duration for every accepted type. Walks the frame headers
    (1024 samples per raw data block) without decoding; a leading ID3v2 tag is
    skipped. Reads by content, not suffix, so held ``.part`` files work.
    """
    samples = 0
    rate = 0
    with open(path, "rb") as fh:
        size = fh.seek(0, 2)
        fh.seek(0)
        offset = _id3v2_end(fh.read(10))
        while True:
            fh.seek(offset)
            frame = _adts_frame(fh.read(7))
            if frame is None or (rate and frame[0] != rate):
                break
            if offset + frame[1] > size:
                break  # truncated: the header promises bytes the file lacks
            rate = frame[0]
            samples += frame[2]
            offset += frame[1]
    if not rate or not samples:
        return None
    return samples / rate


def _id3v2_end(head: bytes) -> int:
    """Byte offset just past a leading ID3v2 tag, or 0 when there is none."""
    if len(head) < 10 or head[:3] != b"ID3":
        return 0
    size = 0
    for byte in head[6:10]:
        size = (size << 7) | (byte & 0x7F)
    return 10 + size + (10 if head[5] & 0x10 else 0)


def _adts_frame(hdr: bytes) -> tuple[int, int, int] | None:
    """(sample rate, frame bytes, samples) of one ADTS header, or None."""
    if len(hdr) < 7 or hdr[0] != 0xFF or (hdr[1] & 0xF6) != 0xF0:
        return None
    rate_index = (hdr[2] >> 2) & 0x0F
    frame_len = ((hdr[3] & 0x03) << 11) | (hdr[4] << 3) | (hdr[5] >> 5)
    if rate_index >= len(_ADTS_RATES) or frame_len < 7:
        return None
    return _ADTS_RATES[rate_index], frame_len, 1024 * ((hdr[6] & 0x03) + 1)


def first_other(tag: TinyTag, key: str) -> str | None:
    """First non-empty value of tinytag's ``other`` field ``key`` (lowercase)."""
    values = (tag.other or {}).get(key) or ()
    for value in values:
        text = str(value).strip()
        if text:
            return text
    return None


__all__ = [
    "HAS_TAG_READER",
    "TagReadError",
    "adts_duration",
    "can_read",
    "first_other",
    "read",
    "require",
]
