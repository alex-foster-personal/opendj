"""Unified tag read wrapper. Writing tags into audio files is removed.

Reads go through :mod:`apps.shared._tagreader` (tinytag, MIT). Writing used
to go through ``mutagen`` (GPL-2.0-or-later). This Apache-2.0 product does
not depend on it, so :func:`write_tags` raises :class:`TagWriteRemoved` and
does not modify the file.

Containers this reader refuses (historical writer scope): ``.aiff`` / ``.wav``.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

POPM_EMAIL = "music-dj-tools@local"
POPM_BUCKETS: dict[int, int] = {0: 0, 1: 51, 2: 102, 3: 153, 4: 204, 5: 255}

TAG_WRITE_REMOVED = (
    "writing tags into audio files was removed because mutagen is "
    "GPL-2.0-or-later; the Apache-2.0 product does not depend on it"
)


class TagWriteRemoved(RuntimeError):
    """Tag writing into an audio file is not available."""

    def __init__(self, detail: str = TAG_WRITE_REMOVED) -> None:
        super().__init__(detail)


class UnsupportedContainer(RuntimeError):
    """Raised for ``.aiff`` / ``.wav``."""


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

    Only fields with an explicit value were written when writing existed.
    A ``None`` means "leave alone".
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


_UNSUPPORTED = {".aiff", ".aif", ".wav"}


def _ext(path: Path) -> str:
    return path.suffix.lower()


def _other_first(other: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        for candidate in (key, key.lower(), key.upper()):
            val = other.get(candidate)
            if val is None:
                continue
            if isinstance(val, (list, tuple)):
                if not val:
                    continue
                val = val[0]
            text = str(val).strip()
            if text:
                return text
    return None


def _safe_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _safe_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _popm_to_rating(popm: int | None) -> int | None:
    if popm is None:
        return None
    best = min(POPM_BUCKETS.items(), key=lambda kv: abs(kv[1] - popm))
    return best[0]


def read_tags(path: Path) -> TagRead:
    """Read tags from ``path`` via tinytag.

    Raises :class:`UnsupportedContainer` for ``.aiff`` / ``.wav``.
    """
    ext = _ext(path)
    if ext in _UNSUPPORTED:
        raise UnsupportedContainer(f"Unsupported container: {ext}")
    from apps.shared._tagreader import read as read_audio

    tag = read_audio(path, image=False, duration=False)
    other = getattr(tag, "other", None) or {}
    rating_raw = _safe_int(_other_first(other, "rating", "popm"))
    if rating_raw is not None and rating_raw > 5:
        rating_raw = _popm_to_rating(rating_raw)
    return TagRead(
        title=(tag.title or None),
        artist=(tag.artist or None),
        album=(tag.album or None),
        genre=(tag.genre or None),
        bpm=_safe_float(_other_first(other, "bpm", "tbpm")),
        key_openkey=_other_first(other, "initialkey", "key", "tkey"),
        key_camelot=_other_first(other, "camelot"),
        energy=_safe_int(_other_first(other, "energy")),
        rating=rating_raw,
        isrc=_other_first(other, "isrc"),
        raw={k: v for k, v in other.items()},
    )


def write_tags(
    path: Path, unified: UnifiedTags, *, dry_run: bool = True
) -> WriteResult:
    """Refuse to write tags. Does not open or modify ``path``.

    ``dry_run`` does not preview a write: writing is not a product feature.
    """
    del unified, dry_run
    raise TagWriteRemoved(
        f"{TAG_WRITE_REMOVED} (refusing {path.name})"
    )
