"""Filesystem audio scanner + lightweight metadata reader (mutagen)."""
from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

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
    genre: str | None = None
    comment: str | None = None
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
    except Exception:  # noqa: BLE001 - malformed tags must not stop scanning
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
        genre=_first(f, "genre"),
        comment=_first(f, "comment"),
        duration_s=float(info.length) if info and getattr(info, "length", None) else None,
        bitrate_kbps=bitrate_kbps,
        sample_rate=int(info.sample_rate) if info and getattr(info, "sample_rate", None) else None,
    )


_RASTER_MAGIC_BY_MIME: dict[str, tuple[bytes, ...]] = {
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "image/bmp": (b"BM",),
}
MAX_EMBEDDED_ARTWORK_BYTES = 4 * 1024 * 1024


def _is_valid_webp(header: bytes) -> bool:
    """``RIFF`` alone is a container signature shared with WAV/AVI/etc, not a
    format signature -- the ``WEBP`` marker at bytes 8-11 is what actually
    says "this RIFF payload is an image". Checking only the ``RIFF`` prefix
    would let a WAV or AVI declared as ``image/webp`` pass the gate.
    """
    return header[:4] == b"RIFF" and header[8:12] == b"WEBP"


def _is_safe_raster_image(header: bytes, mime: str) -> bool:
    """The declared mime must be allow-listed AND the bytes must match THAT
    format's own magic number, not merely any raster format's.

    A crafted APIC/covr frame can declare any mime it likes (e.g.
    ``text/html``); trusting it verbatim as the HTTP ``Content-Type`` lets a
    same-origin artwork URL render attacker-controlled markup. Pairing the
    mime to its own magic bytes (rather than checking membership in the
    allow-list and the union of all magic numbers separately) closes the gap
    where e.g. ``mime="image/png"`` with JPEG bytes would otherwise pass.
    """
    if mime == "image/webp":
        return _is_valid_webp(header)
    magics = _RASTER_MAGIC_BY_MIME.get(mime)
    return magics is not None and header.startswith(magics)


def _safe_picture_mime(data: object, mime: str) -> str | None:
    """Normalized mime when ``data`` is a bounded, safe raster payload.

    Mutagen already owns the frame payload. Check its length and only copy the
    tiny magic-byte prefix before materializing a response-sized ``bytes``
    object, so oversized tag frames cannot multiply the process's memory use.
    """
    try:
        if len(data) > MAX_EMBEDDED_ARTWORK_BYTES:  # type: ignore[arg-type]
            return None
        header = bytes(data[:12])  # type: ignore[index]
    except (TypeError, ValueError):
        return None
    normalized_mime = mime.strip().lower()
    return normalized_mime if _is_safe_raster_image(header, normalized_mime) else None


def _as_bytes(data: object) -> bytes | None:
    if isinstance(data, bytes):
        return data
    try:
        return bytes(cast(bytes | bytearray | memoryview, data))
    except (TypeError, ValueError):
        return None


def _first_safe_picture(
    candidates: Iterator[tuple[object, str, int | None]],
) -> tuple[bytes, str] | None:
    """First SAFE candidate, but a type-3 (front cover) safe candidate wins
    over an earlier non-cover one.

    Multiple embedded pictures are valid (FLAC ``pictures``, ID3 ``APIC``)
    and their ordering is file-controlled, not a signal -- an unconditional
    "take the first one" lets an invalid or non-cover first frame hide a
    perfectly usable front cover a frame or two later.

    Mime tokens are case-insensitive per RFC 2045; a tag author writing
    ``IMAGE/JPEG`` is declaring the same type as ``image/jpeg``, so the
    allow-list lookup normalizes case here rather than at every caller. The
    normalized form is also what gets served as the HTTP ``Content-Type``.
    """
    fallback: tuple[object, str] | None = None
    for data, mime, pic_type in candidates:
        normalized_mime = _safe_picture_mime(data, mime)
        if normalized_mime is None:
            continue
        if pic_type == 3:
            payload = _as_bytes(data)
            return (payload, normalized_mime) if payload is not None else None
        if fallback is None:
            fallback = (data, normalized_mime)
    if fallback is None:
        return None
    payload = _as_bytes(fallback[0])
    return (payload, fallback[1]) if payload is not None else None


def _has_safe_picture(candidates: Iterator[tuple[object, str, int | None]]) -> bool:
    """Whether a bounded, safe embedded picture exists without copying it."""
    return any(_safe_picture_mime(data, mime) is not None for data, mime, _ in candidates)


def _picture_candidates(path: Path) -> Iterator[tuple[object, str, int | None]]:
    """Every embedded picture as ``(data, declared mime, type)``, read by tinytag.

    tinytag (MIT) reads ID3 ``APIC`` (mp3/wav/aiff), FLAC and Ogg picture
    blocks and the MP4 ``covr`` atom. mutagen did this before, but it is GPL,
    so the packaged app never shipped it and every local import read no art
    there (issue #4717). The front cover is reported as type 3 so
    :func:`_first_safe_picture` prefers it. A file tinytag cannot parse
    yields nothing.
    """
    from tinytag import TinyTag, TinyTagException

    try:
        tag = TinyTag.get(str(path), tags=True, duration=False, image=True)
    except (TinyTagException, OSError, ValueError):
        return
    for kind, images in tag.images.as_dict().items():
        picture_type = 3 if kind == "front_cover" else None
        for image in images:
            yield image.data, image.mime_type or "", picture_type


def read_embedded_artwork(path: Path) -> tuple[bytes, str] | None:
    """Real cover-art bytes + mime type embedded in ``path``'s tags, or ``None``.

    ``None`` when the file cannot be parsed, has no picture, or every
    picture fails :func:`_is_safe_raster_image` -- never a synthesised or
    placeholder image, and never a tag-declared mime trusted verbatim into
    an HTTP response.
    """
    return _first_safe_picture(_picture_candidates(path))


def embedded_artwork_available(path: Path) -> bool:
    """Whether ``path`` contains a bounded safe picture."""
    return _has_safe_picture(_picture_candidates(path))


__all__ = [
    "AudioFile",
    "AudioMetadata",
    "embedded_artwork_available",
    "read_embedded_artwork",
    "read_metadata",
    "scan_music_files",
]
