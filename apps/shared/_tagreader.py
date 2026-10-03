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
from typing import TYPE_CHECKING, NamedTuple

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


_MP4_ALIASES = frozenset({".alac"})


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

    suffix = Path(path).suffix.lower()
    # .alac is an MP4 container: tinytag's extension list omits it, but its
    # content sniff parses it like .m4a (measured on tinytag 2.3.2).
    return suffix in TinyTag.SUPPORTED_FILE_EXTENSIONS or suffix in _MP4_ALIASES


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
        # tinytag misreads raw ADTS streams as MPEG (0.03 s for a 7.3 s file)
        # rather than failing, and fills bitrate, samplerate and channels
        # from the same bogus frames; every stream property comes from the
        # frame walk instead.
        _apply_adts(path, tag)
    return tag


def _apply_adts(path: Path | str, tag: TinyTag) -> None:
    """Overwrite tinytag's stream properties from the ADTS frame walk.

    A file that opens with an ADTS frame but fails the walk (truncated,
    corrupt, mixed rates) raises rather than keeping tinytag's bogus values.
    """
    try:
        stream = adts_stream(path)
        damaged = stream is None and starts_with_adts(path)
    except OSError as exc:  # removed or unreadable since tinytag closed it
        raise TagReadError(str(exc) or type(exc).__name__) from exc
    if damaged:
        raise TagReadError("damaged ADTS stream: the frame walk failed")
    if stream is None:
        return
    tag.duration = stream.duration
    tag.samplerate = stream.samplerate
    tag.channels = stream.channels
    tag.bitrate = stream.bitrate


_ADTS_RATES = (
    96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000,
    11025, 8000, 7350,
)


class AdtsStream(NamedTuple):
    """Stream properties of a raw ADTS AAC file, from its frame headers."""

    duration: float
    samplerate: int
    channels: int
    bitrate: float  # kbps over the audio frames, tags excluded


def adts_duration(path: Path | str) -> float | None:
    """Duration in seconds of a raw ADTS AAC stream, or None if it is not one.

    tinytag has no ADTS reader (see :func:`can_read`), and the upload duplicate
    check needs a duration for every accepted type. Reads by content, not
    suffix, so held ``.part`` files work.
    """
    stream = adts_stream(path)
    return stream.duration if stream else None


def adts_stream(path: Path | str) -> AdtsStream | None:
    """Walk the ADTS frame headers (1024 samples per raw data block) without
    decoding; a leading ID3v2 tag is skipped and a trailing non-ADTS tag ends
    the walk. None when the file is not ADTS, or a frame is truncated or
    changes sample rate, since the frames walked would not be the stream.
    """
    samples = 0
    first: tuple[int, int, int, int] | None = None
    with open(path, "rb") as fh:
        size = fh.seek(0, 2)
        fh.seek(0)
        start = offset = _id3v2_end(fh.read(10))
        while True:
            fh.seek(offset)
            hdr = fh.read(_TAIL_PROBE)  # enough for the longest tag signature
            frame = _adts_frame(hdr)
            if frame is None:
                if hdr and not hdr.startswith(_TRAILING_TAGS):
                    return None  # damaged frame, not EOF or a trailing tag
                break
            if offset + frame[1] > size or (first and frame[0] != first[0]):
                return None
            first = first or frame
            samples += frame[2]
            offset += frame[1]
    if first is None:
        return None
    duration = samples / first[0]
    kbps = (offset - start) * 8 / duration / 1000
    return AdtsStream(duration, first[0], first[3], kbps)


def starts_with_adts(path: Path | str) -> bool:
    """True when the bytes past any leading ID3v2 tag carry the ADTS sync word.

    Only the sync word and the zero layer bits are checked, not a whole valid
    header, so a damaged first frame still reads as ADTS (and is refused)
    rather than falling back to tinytag. MPEG audio sets nonzero layer bits,
    so an mp3 never matches. A leading ID3v2 tag that claims more bytes than
    the file holds hides whatever follows it, so it also counts: the file is
    damaged either way and must be refused, not read as "not ADTS".
    """
    with open(path, "rb") as fh:
        size = fh.seek(0, 2)
        fh.seek(0)
        start = _id3v2_end(fh.read(10))
        if start > size:
            return True
        fh.seek(start)
        head = fh.read(2)
    return len(head) == 2 and head[0] == 0xFF and (head[1] & 0xF6) == 0xF0


def _id3v2_end(head: bytes) -> int:
    """Byte offset just past a leading ID3v2 tag, or 0 when there is none."""
    if len(head) < 10 or head[:3] != b"ID3":
        return 0
    size = 0
    for byte in head[6:10]:
        size = (size << 7) | (byte & 0x7F)
    return 10 + size + (10 if head[5] & 0x10 else 0)


# Metadata a raw AAC file may carry after its last frame: ID3v1, ID3v2
# (appended), APEv2 and Lyrics3. Anything else after a frame is damage.
_TRAILING_TAGS = (b"TAG", b"ID3", b"APETAGEX", b"LYRICSBEGIN")
_TAIL_PROBE = max(7, *(len(sig) for sig in _TRAILING_TAGS))


def _adts_frame(hdr: bytes) -> tuple[int, int, int, int] | None:
    """(sample rate, frame bytes, samples, channels) of one ADTS header."""
    if len(hdr) < 7 or hdr[0] != 0xFF or (hdr[1] & 0xF6) != 0xF0:
        return None
    rate_index = (hdr[2] >> 2) & 0x0F
    frame_len = ((hdr[3] & 0x03) << 11) | (hdr[4] << 3) | (hdr[5] >> 5)
    if rate_index >= len(_ADTS_RATES) or frame_len < 7:
        return None
    channels = ((hdr[2] & 0x01) << 2) | (hdr[3] >> 6)
    return _ADTS_RATES[rate_index], frame_len, 1024 * ((hdr[6] & 0x03) + 1), channels


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
    "AdtsStream",
    "TagReadError",
    "adts_duration",
    "adts_stream",
    "can_read",
    "first_other",
    "read",
    "require",
    "starts_with_adts",
]
