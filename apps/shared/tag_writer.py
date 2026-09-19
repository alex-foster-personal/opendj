"""Unified tag read/write wrapper on top of mutagen (Phase 7 Plan 02).

Handles three container families:

* **ID3v2.4** -- MP3 (``.mp3``)
* **MP4 atoms** -- M4A / MP4 (``.m4a``, ``.mp4``, ``.aac``, ``.alac``)
* **Vorbis comments** -- FLAC / OGG (``.flac``, ``.ogg``)

The writer is deterministic and mutagen-only; there is no external
binary or network call. Dry-run is supported at the API layer so callers
can preview planned changes.

Design constraints:

* Never rename the file. Only tag blocks change.
* POPM rating mapping (RB + Windows-compatible):
  ``{0: 0, 1: 51, 2: 102, 3: 153, 4: 204, 5: 255}``.
* Raises :class:`UnsupportedContainer` for ``.aiff`` / ``.wav``.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

POPM_EMAIL = "music-dj-tools@local"
POPM_BUCKETS: dict[int, int] = {0: 0, 1: 51, 2: 102, 3: 153, 4: 204, 5: 255}


class UnsupportedContainer(RuntimeError):
    """Raised for ``.aiff`` / ``.wav`` (and anything else mutagen-unseen)."""


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
    from mutagen.id3 import ID3, ID3NoHeaderError  # type: ignore

    try:
        t = ID3(str(path))
    except ID3NoHeaderError:
        return TagRead(raw={})
    raw = {k: str(t.get(k)) for k in t.keys()}
    popm = None
    for f in t.getall("POPM"):
        popm = f.rating
        break
    return TagRead(
        title=_first(t.get("TIT2")),
        artist=_first(t.get("TPE1")),
        album=_first(t.get("TALB")),
        genre=_first(t.get("TCON")),
        bpm=_safe_float(_first(t.get("TBPM"))),
        key_openkey=_first(t.get("TKEY")),
        key_camelot=_first_txxx(t, "Camelot"),
        energy=_safe_int(_first_txxx(t, "ENERGY")),
        rating=_popm_to_rating(popm),
        isrc=_first(t.get("TSRC")),
        raw=raw,
    )


def _read_mp4(path: Path) -> TagRead:
    from mutagen.mp4 import MP4  # type: ignore

    t = MP4(str(path)).tags or {}
    raw = {k: str(v) for k, v in t.items()}
    bpm_val = t.get("tmpo")
    bpm_num = float(bpm_val[0]) if bpm_val else None
    rating_atom = t.get("----:com.apple.iTunes:RATING")
    rating_val = None
    if rating_atom:
        try:
            rating_val = int(rating_atom[0].decode("utf-8"))
        except Exception:
            rating_val = None
    return TagRead(
        title=_first(t.get("\xa9nam")),
        artist=_first(t.get("\xa9ART")),
        album=_first(t.get("\xa9alb")),
        genre=_first(t.get("\xa9gen")),
        bpm=bpm_num,
        key_openkey=_first_freeform(t, "INITIALKEY"),
        key_camelot=_first_freeform(t, "CAMELOT"),
        energy=_safe_int(_first_freeform(t, "ENERGY")),
        rating=rating_val,
        isrc=_first_freeform(t, "ISRC"),
        raw=raw,
    )


def _read_flac_ogg(path: Path) -> TagRead:
    from mutagen import File as MFile  # type: ignore

    f = MFile(str(path))
    tags_obj = f.tags
    raw = {k: str(v) for k, v in (tags_obj or {})}

    def g(k: str) -> str | None:
        # Vorbis comments are case-insensitive per spec; mutagen normalises
        # to lowercase on read. Try both cases to be safe.
        if tags_obj is None:
            return None
        val = tags_obj.get(k.upper()) or tags_obj.get(k.lower())
        return _first(val)

    rating_raw = g("RATING")
    rating_val = None
    if rating_raw is not None:
        try:
            rating_val = int(float(rating_raw))
        except ValueError:
            rating_val = None
    return TagRead(
        title=g("TITLE"),
        artist=g("ARTIST"),
        album=g("ALBUM"),
        genre=g("GENRE"),
        bpm=_safe_float(g("BPM")),
        key_openkey=g("KEY"),
        key_camelot=g("CAMELOT"),
        energy=_safe_int(g("ENERGY")),
        rating=rating_val,
        isrc=g("ISRC"),
        raw=raw,
    )


def read_tags(path: Path) -> TagRead:
    """Read tags from ``path``. Raises :class:`UnsupportedContainer`
    for ``.aiff`` / ``.wav``.

    Raises :class:`ImportError` when the optional ``mutagen`` dep is not
    installed (``pip install 'music-dj-tools[tags]'``).
    """
    from ._mutagen import require as _require_mutagen

    _require_mutagen()
    ext = _ext(path)
    if ext in _UNSUPPORTED:
        raise UnsupportedContainer(f"Unsupported container: {ext}")
    if ext in _MP3:
        return _read_mp3(path)
    if ext in _MP4:
        return _read_mp4(path)
    if ext in _FLAC or ext in _OGG:
        return _read_flac_ogg(path)
    # Fallback: try mutagen File generic (may still succeed for edge formats).
    from mutagen import File as MFile  # type: ignore

    f = MFile(str(path))
    if f is None:
        return TagRead()
    return TagRead(raw={k: str(v) for k, v in (f.tags or {}).items()})


# ---------------------------------------------------------------- write


def _write_mp3(path: Path, u: UnifiedTags, *, dry_run: bool) -> WriteResult:
    from mutagen.id3 import (  # type: ignore
        ID3,
        POPM,
        TALB,
        TBPM,
        TCON,
        TIT2,
        TKEY,
        TPE1,
        TSRC,
        TXXX,
        ID3NoHeaderError,
    )

    try:
        t = ID3(str(path))
    except ID3NoHeaderError:
        t = ID3()
    applied: dict[str, Any] = {}
    if u.title is not None:
        t["TIT2"] = TIT2(encoding=3, text=u.title); applied["title"] = u.title
    if u.artist is not None:
        t["TPE1"] = TPE1(encoding=3, text=u.artist); applied["artist"] = u.artist
    if u.album is not None:
        t["TALB"] = TALB(encoding=3, text=u.album); applied["album"] = u.album
    if u.genre is not None:
        t["TCON"] = TCON(encoding=3, text=u.genre); applied["genre"] = u.genre
    if u.bpm is not None:
        t["TBPM"] = TBPM(encoding=3, text=str(round(u.bpm))); applied["bpm"] = u.bpm
    if u.key_openkey is not None:
        t["TKEY"] = TKEY(encoding=3, text=u.key_openkey); applied["key_openkey"] = u.key_openkey
    if u.key_camelot is not None:
        t.delall("TXXX:CAMELOT")
        t.add(TXXX(encoding=3, desc="CAMELOT", text=u.key_camelot))
        applied["key_camelot"] = u.key_camelot
    if u.energy is not None:
        t.delall("TXXX:ENERGY")
        t.add(TXXX(encoding=3, desc="ENERGY", text=str(u.energy)))
        applied["energy"] = u.energy
    if u.isrc is not None:
        t["TSRC"] = TSRC(encoding=3, text=u.isrc); applied["isrc"] = u.isrc
    if u.rating is not None:
        popm_val = _rating_to_popm(u.rating) or 0
        t.delall("POPM")
        t.add(POPM(email=POPM_EMAIL, rating=popm_val, count=0))
        applied["rating"] = u.rating
    if not dry_run:
        t.save(str(path), v2_version=4)
    return WriteResult(path=path, applied=applied, dry_run=dry_run)


def _write_mp4(path: Path, u: UnifiedTags, *, dry_run: bool) -> WriteResult:
    from mutagen.mp4 import MP4, MP4FreeForm  # type: ignore

    f = MP4(str(path))
    if f.tags is None:
        f.add_tags()
    t = f.tags
    applied: dict[str, Any] = {}

    def ff(_name: str, value: str) -> MP4FreeForm:
        return MP4FreeForm(value.encode("utf-8"), dataformat=MP4FreeForm.FORMAT_TEXT)

    if u.title is not None:
        t["\xa9nam"] = [u.title]; applied["title"] = u.title
    if u.artist is not None:
        t["\xa9ART"] = [u.artist]; applied["artist"] = u.artist
    if u.album is not None:
        t["\xa9alb"] = [u.album]; applied["album"] = u.album
    if u.genre is not None:
        t["\xa9gen"] = [u.genre]; applied["genre"] = u.genre
    if u.bpm is not None:
        t["tmpo"] = [round(u.bpm)]; applied["bpm"] = u.bpm
    if u.key_openkey is not None:
        t["----:com.apple.iTunes:INITIALKEY"] = [ff("INITIALKEY", u.key_openkey)]
        applied["key_openkey"] = u.key_openkey
    if u.key_camelot is not None:
        t["----:com.apple.iTunes:CAMELOT"] = [ff("CAMELOT", u.key_camelot)]
        applied["key_camelot"] = u.key_camelot
    if u.energy is not None:
        t["----:com.apple.iTunes:ENERGY"] = [ff("ENERGY", str(u.energy))]
        applied["energy"] = u.energy
    if u.rating is not None:
        t["----:com.apple.iTunes:RATING"] = [ff("RATING", str(u.rating))]
        applied["rating"] = u.rating
    if u.isrc is not None:
        t["----:com.apple.iTunes:ISRC"] = [ff("ISRC", u.isrc)]
        applied["isrc"] = u.isrc
    if not dry_run:
        f.save()
    return WriteResult(path=path, applied=applied, dry_run=dry_run)


def _write_flac_ogg(path: Path, u: UnifiedTags, *, dry_run: bool) -> WriteResult:
    from mutagen import File as MFile  # type: ignore

    f = MFile(str(path))
    if f is None:
        raise UnsupportedContainer(f"mutagen cannot open: {path}")
    applied: dict[str, Any] = {}
    t = f.tags

    def setv(k: str, v: str) -> None:
        t[k] = [v]

    if u.title is not None:
        setv("TITLE", u.title); applied["title"] = u.title
    if u.artist is not None:
        setv("ARTIST", u.artist); applied["artist"] = u.artist
    if u.album is not None:
        setv("ALBUM", u.album); applied["album"] = u.album
    if u.genre is not None:
        setv("GENRE", u.genre); applied["genre"] = u.genre
    if u.bpm is not None:
        setv("BPM", str(round(u.bpm))); applied["bpm"] = u.bpm
    if u.key_openkey is not None:
        setv("KEY", u.key_openkey); applied["key_openkey"] = u.key_openkey
    if u.key_camelot is not None:
        setv("CAMELOT", u.key_camelot); applied["key_camelot"] = u.key_camelot
    if u.energy is not None:
        setv("ENERGY", str(u.energy)); applied["energy"] = u.energy
    if u.rating is not None:
        setv("RATING", str(u.rating)); applied["rating"] = u.rating
    if u.isrc is not None:
        setv("ISRC", u.isrc); applied["isrc"] = u.isrc
    if not dry_run:
        f.save()
    return WriteResult(path=path, applied=applied, dry_run=dry_run)


def write_tags(
    path: Path, unified: UnifiedTags, *, dry_run: bool = True
) -> WriteResult:
    """Dispatch to the right backend; return a :class:`WriteResult`.

    Raises :class:`ImportError` when the optional ``mutagen`` dep is not
    installed (``pip install 'music-dj-tools[tags]'``).
    """
    from ._mutagen import require as _require_mutagen

    _require_mutagen()
    ext = _ext(path)
    if ext in _UNSUPPORTED:
        raise UnsupportedContainer(f"Unsupported container: {ext}")
    if ext in _MP3:
        return _write_mp3(path, unified, dry_run=dry_run)
    if ext in _MP4:
        return _write_mp4(path, unified, dry_run=dry_run)
    if ext in _FLAC or ext in _OGG:
        return _write_flac_ogg(path, unified, dry_run=dry_run)
    raise UnsupportedContainer(f"No tag writer for: {ext}")


# ------------------------------------------------------------- helpers


def _first(val: Any) -> str | None:
    if val is None:
        return None
    if isinstance(val, list) and val:
        val = val[0]
    if val is None:
        return None
    s = str(val).strip()
    return s or None


def _first_txxx(id3, desc: str) -> str | None:
    for frame in id3.getall("TXXX"):
        if getattr(frame, "desc", "").upper() == desc.upper():
            return _first(list(frame.text) if hasattr(frame, "text") else [frame])
    return None


def _first_freeform(tags, key: str) -> str | None:
    full = f"----:com.apple.iTunes:{key}"
    val = tags.get(full)
    if not val:
        return None
    try:
        return val[0].decode("utf-8") if hasattr(val[0], "decode") else str(val[0])
    except Exception:
        return None


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
