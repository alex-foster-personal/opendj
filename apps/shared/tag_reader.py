"""The one place this codebase READS audio tags: tinytag (MIT), a core dependency.

Why tinytag and not mutagen: mutagen is GPL-2.0-or-later and this project is
Apache-2.0, so mutagen was an opt-in ``[tags]`` extra the packaged app never
shipped -- and every folder import in the installed app therefore read no tags
at all. tinytag is MIT, pure Python, has no dependencies, and reads every
container the library scanner allowlists (MP3/ID3v2, FLAC, MP4/M4A, AIFF, WAV,
OGG Vorbis, Opus). See ``docs/decisions/ADR-NEW-permissive-audio-tag-io.md``.

Writes do NOT go through here: tinytag is read-only. ID3v2 (MP3), FLAC, MP4
and Ogg writes live in :mod:`apps.shared.id3v2`, :mod:`apps.shared.flac_meta`,
:mod:`apps.shared.mp4_meta` and :mod:`apps.shared.ogg_comment`.

Mini-PRD
--------
✔︎ R1 read title/artist/album/genre/comment/BPM/key/ISRC/duration/bitrate/
       sample rate from MP3, FLAC, M4A, AIFF, WAV, OGG and Opus files.
  [if] a file carries a tag   [then] its value is returned verbatim, stripped
  [if] a tag is absent        [then] the field is ``None``, never ``""``
  [if] BPM is not a number    [then] ``bpm`` is ``None`` rather than a guess
✔︎ R2 failures are explicit: :func:`read_tags` raises :class:`TagReadError`
       naming the file and the cause; it never returns an empty record for a
       file it could not parse.
  [if] the file is not audio  [then] TagReadError (tinytag returns an all-None
       record for some garbage instead of raising; that counts as a failure)
  [if] the file is missing    [then] TagReadError
✔︎ R3 embedded pictures are exposed with their declared mime and whether they
       are the front cover; safety screening stays in ``audio_files``.
"""
from __future__ import annotations

import math
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from tinytag import TinyTag, TinyTagException

FRONT_COVER_PICTURE_TYPE = 3
"""ID3 APIC / FLAC PICTURE type for the front cover (ID3v2.4 section 4.14)."""


class TagReadError(Exception):
    """A file's tags could not be read. Carries the path and the cause."""

    def __init__(self, path: Path, cause: str) -> None:
        self.path = path
        self.cause = cause
        super().__init__(f"cannot read tags from {path}: {cause}")


@dataclass(frozen=True, slots=True)
class FileTags:
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    genre: str | None = None
    comment: str | None = None
    bpm: float | None = None
    key: str | None = None
    isrc: str | None = None
    duration_s: float | None = None
    bitrate_kbps: int | None = None
    sample_rate: int | None = None
    #: Every non-standard field tinytag surfaced, keyed by its lowercased name
    #: (e.g. ``camelot``, ``energy`` from TXXX / Vorbis / iTunes free-form).
    other: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def first_other(self, name: str) -> str | None:
        values = self.other.get(name.lower())
        return _clean(values[0]) if values else None


# ----- public API -------------------------------------------------------------
def read_tags(path: Path) -> FileTags:
    """Every tag and stream property tinytag reads from ``path``.

    Raises :class:`TagReadError` when the file cannot be parsed or opened.
    """
    tag = _load(path, image=False)
    if _found_nothing(tag):
        raise TagReadError(path, "no audio stream and no tags found")
    other = {name.lower(): tuple(str(v) for v in values) for name, values in tag.other.items()}
    return FileTags(
        title=_clean(tag.title),
        artist=_clean(tag.artist),
        album=_clean(tag.album),
        genre=_clean(tag.genre),
        comment=_clean(tag.comment) or _first(other, "_comment"),
        bpm=_parse_bpm(_first(other, "bpm")),
        key=_first(other, "initial_key"),
        isrc=_first(other, "isrc"),
        duration_s=_positive_float(tag.duration),
        bitrate_kbps=int(tag.bitrate) if _positive_float(tag.bitrate) else None,
        sample_rate=int(tag.samplerate) if tag.samplerate else None,
        other=other,
    )


def embedded_pictures(path: Path) -> Iterator[tuple[bytes, str, int | None]]:
    """``(data, declared_mime, picture_type)`` for every embedded picture.

    ``picture_type`` is :data:`FRONT_COVER_PICTURE_TYPE` for a front cover and
    ``None`` otherwise. The mime is the file's own declaration (``""`` when it
    declares none) and is NOT trusted here: ``audio_files`` checks it against
    the bytes before anything is served.
    """
    tag = _load(path, image=True)
    for kind, images in tag.images.as_dict().items():
        picture_type = FRONT_COVER_PICTURE_TYPE if kind == "front_cover" else None
        for image in images:
            yield image.data, image.mime_type or "", picture_type


# ----- helpers ----------------------------------------------------------------
def _load(path: Path, *, image: bool) -> TinyTag:
    try:
        return TinyTag.get(str(path), image=image)
    except (TinyTagException, OSError) as exc:
        raise TagReadError(path, f"{type(exc).__name__}: {exc}") from exc


def _found_nothing(tag: TinyTag) -> bool:
    """tinytag answers an unparseable MP3 / M4A / WAV with an all-``None``
    record instead of raising; that is a failed read, not an untagged file."""
    stated = (tag.duration, tag.samplerate, tag.title, tag.artist, tag.album, tag.genre, tag.comment)
    return all(value is None for value in stated) and not tag.other


def _clean(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\x00", "").strip()
    return text or None


def _first(other: Mapping[str, tuple[str, ...]], name: str) -> str | None:
    values = other.get(name)
    return _clean(values[0]) if values else None


def _positive_float(value: object) -> float | None:
    if not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) and number > 0 else None


def _parse_bpm(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return _positive_float(float(raw.replace(",", ".")))
    except ValueError:
        return None


__all__ = [
    "FRONT_COVER_PICTURE_TYPE",
    "FileTags",
    "TagReadError",
    "embedded_pictures",
    "read_tags",
]
