"""Unified tag read/write wrapper (Phase 7 Plan 02), licence-clean.

Reads go through :mod:`apps.shared.tag_reader` (tinytag, MIT) for every
container, except MP3 where the in-house :mod:`apps.shared.id3v2` reader is
used so POPM ratings and TXXX frames are exact. Writes:

* **ID3v2** -- MP3 (``.mp3``) via :mod:`apps.shared.id3v2` (in-house).
* **Vorbis comments** -- FLAC (``.flac``) via :mod:`apps.shared.flac_meta`
  (in-house).
* **MP4 atoms / Ogg** -- ``.m4a`` / ``.mp4`` / ``.aac`` / ``.alac`` / ``.ogg``
  are READ but writing them raises :class:`UnsupportedContainer`. The writer
  that covered them was the GPL ``mutagen``, removed for licensing (see
  ``docs/decisions/ADR-NEW-permissive-audio-tag-io.md``); rewriting an MP4
  ``moov`` (chunk offsets) or repaging an Ogg stream is not a safe in-house
  one-off, so it is refused by name rather than attempted.

Design constraints:

* Never rename the file. Only tag blocks change, via temp file + replace.
* POPM rating mapping (RB + Windows-compatible):
  ``{0: 0, 1: 51, 2: 102, 3: 153, 4: 204, 5: 255}``.
* Raises :class:`UnsupportedContainer` for ``.aiff`` / ``.wav``.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import flac_meta, id3v2, tag_reader

POPM_EMAIL = "music-dj-tools@local"
POPM_BUCKETS: dict[int, int] = {0: 0, 1: 51, 2: 102, 3: 153, 4: 204, 5: 255}


class UnsupportedContainer(RuntimeError):
    """Raised for a container this module cannot read or write."""


@dataclass(frozen=True)
class TagRead:
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    genre: str | None = None
    bpm: float | None = None
    key_openkey: str | None = None
    key_camelot: str | None = None
    energy: int | None = None
    rating: int | None = None  # 0..5 user-facing
    isrc: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class UnifiedTags:
    """Plan computed by ``apps.tags.unify``.

    Only fields with an explicit value are written; a ``None`` means
    "leave alone". Callers can build a partial plan (e.g. genre + bpm
    only) without touching title / artist.
    """

    title: str | None = None
    artist: str | None = None
    album: str | None = None
    genre: str | None = None
    bpm: float | None = None
    key_openkey: str | None = None
    key_camelot: str | None = None
    energy: int | None = None
    rating: int | None = None
    isrc: str | None = None


@dataclass(frozen=True)
class WriteResult:
    path: Path
    applied: dict[str, Any] = field(default_factory=dict)
    skipped: dict[str, Any] = field(default_factory=dict)
    dry_run: bool = True


# --------------------------------------------------------------- dispatch


_MP3 = {".mp3"}
_MP4 = {".m4a", ".mp4", ".aac", ".alac"}
_FLAC = {".flac"}
_OGG = {".ogg"}
_WRITE_REFUSED = _MP4 | _OGG
_UNSUPPORTED = {".aiff", ".aif", ".wav"}


def _ext(path: Path) -> str:
    return path.suffix.lower()


def _rating_to_popm(rating: int | None) -> int | None:
    if rating is None:
        return None
    if rating < 0 or rating > 5:
        raise ValueError(f"rating out of range: {rating}")
    return POPM_BUCKETS[rating]


def _popm_to_rating(popm: int | None) -> int | None:
    if popm is None:
        return None
    # Choose the closest bucket.
    best = min(POPM_BUCKETS.items(), key=lambda kv: abs(kv[1] - popm))
    return best[0]


# ----------------------------------------------------------------- read


def _read_mp3(path: Path) -> TagRead:
    t = id3v2.read_tag(path)
    if t is None:
        return TagRead(raw={})
    raw = {frame.frame_id: frame.data.hex() for frame in t.frames}
    return TagRead(
        title=t.first_text("TIT2"),
        artist=t.first_text("TPE1"),
        album=t.first_text("TALB"),
        genre=t.first_text("TCON"),
        bpm=_safe_float(t.first_text("TBPM")),
        key_openkey=t.first_text("TKEY"),
        key_camelot=t.txxx("Camelot"),
        energy=_safe_int(t.txxx("ENERGY")),
        rating=_popm_to_rating(t.popm_rating()),
        isrc=t.first_text("TSRC"),
        raw=raw,
    )


def _read_generic(path: Path) -> TagRead:
    """MP4 / FLAC / Ogg through tinytag: standard fields plus custom ones."""
    t = tag_reader.read_tags(path)
    return TagRead(
        title=t.title,
        artist=t.artist,
        album=t.album,
        genre=t.genre,
        bpm=t.bpm,
        key_openkey=t.key,
        key_camelot=t.first_other("camelot"),
        energy=_safe_int(t.first_other("energy")),
        rating=_safe_int(t.first_other("rating")),
        isrc=t.isrc,
        raw={name: list(values) for name, values in t.other.items()},
    )


def read_tags(path: Path) -> TagRead:
    """Read tags from ``path``. Raises :class:`UnsupportedContainer`
    for ``.aiff`` / ``.wav`` and for anything the tag reader cannot parse.
    """
    ext = _ext(path)
    if ext in _UNSUPPORTED:
        raise UnsupportedContainer(f"Unsupported container: {ext}")
    if ext in _MP3:
        return _read_mp3(path)
    try:
        return _read_generic(path)
    except tag_reader.TagReadError as exc:
        raise UnsupportedContainer(str(exc)) from exc


# ---------------------------------------------------------------- write


def _write_mp3(path: Path, u: UnifiedTags, *, dry_run: bool) -> WriteResult:
    t = id3v2.load_or_new(path, new_version=4)
    applied: dict[str, Any] = {}
    for frame_id, field_name, value in (
        ("TIT2", "title", u.title),
        ("TPE1", "artist", u.artist),
        ("TALB", "album", u.album),
        ("TCON", "genre", u.genre),
        ("TKEY", "key_openkey", u.key_openkey),
        ("TSRC", "isrc", u.isrc),
    ):
        if value is not None:
            t.set_text(frame_id, value)
            applied[field_name] = value
    if u.bpm is not None:
        t.set_text("TBPM", str(round(u.bpm)))
        applied["bpm"] = u.bpm
    if u.key_camelot is not None:
        t.set_txxx("CAMELOT", u.key_camelot)
        applied["key_camelot"] = u.key_camelot
    if u.energy is not None:
        t.set_txxx("ENERGY", str(u.energy))
        applied["energy"] = u.energy
    if u.rating is not None:
        t.set_popm(POPM_EMAIL, _rating_to_popm(u.rating) or 0)
        applied["rating"] = u.rating
    if not dry_run:
        id3v2.save(path, t)
    return WriteResult(path=path, applied=applied, dry_run=dry_run)


def _write_flac(path: Path, u: UnifiedTags, *, dry_run: bool) -> WriteResult:
    meta = flac_meta.read(path)
    applied: dict[str, Any] = {}
    for key, field_name, value in (
        ("TITLE", "title", u.title),
        ("ARTIST", "artist", u.artist),
        ("ALBUM", "album", u.album),
        ("GENRE", "genre", u.genre),
        ("KEY", "key_openkey", u.key_openkey),
        ("CAMELOT", "key_camelot", u.key_camelot),
        ("ISRC", "isrc", u.isrc),
    ):
        if value is not None:
            meta.set(key, value)
            applied[field_name] = value
    if u.bpm is not None:
        meta.set("BPM", str(round(u.bpm)))
        applied["bpm"] = u.bpm
    if u.energy is not None:
        meta.set("ENERGY", str(u.energy))
        applied["energy"] = u.energy
    if u.rating is not None:
        meta.set("RATING", str(u.rating))
        applied["rating"] = u.rating
    if not dry_run:
        flac_meta.save(path, meta)
    return WriteResult(path=path, applied=applied, dry_run=dry_run)


def write_tags(
    path: Path, unified: UnifiedTags, *, dry_run: bool = True
) -> WriteResult:
    """Dispatch to the right writer; return a :class:`WriteResult`.

    Raises :class:`UnsupportedContainer` for AIFF / WAV and for the MP4 / Ogg
    containers no in-house writer covers -- also on ``dry_run``, so a preview
    never promises a write that cannot happen.
    """
    ext = _ext(path)
    if ext in _UNSUPPORTED:
        raise UnsupportedContainer(f"Unsupported container: {ext}")
    if ext in _WRITE_REFUSED:
        raise UnsupportedContainer(
            f"writing tags to {ext} files is not supported: MP4 and Ogg tag "
            "writes needed the GPL mutagen, removed for licensing "
            "(docs/decisions/ADR-NEW-permissive-audio-tag-io.md); MP3 and "
            "FLAC writes are supported"
        )
    if ext in _MP3:
        return _write_mp3(path, unified, dry_run=dry_run)
    if ext in _FLAC:
        return _write_flac(path, unified, dry_run=dry_run)
    raise UnsupportedContainer(f"No tag writer for: {ext}")


# ------------------------------------------------------------- helpers


def _safe_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _safe_int(val: Any) -> int | None:
    if val is None:
        return None
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return None


__all__ = [
    "POPM_BUCKETS",
    "POPM_EMAIL",
    "TagRead",
    "UnifiedTags",
    "UnsupportedContainer",
    "WriteResult",
    "read_tags",
    "write_tags",
]
