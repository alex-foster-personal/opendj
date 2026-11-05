"""Playlist thumbnails: one square image per playlist (ARTTHUMB-8728..8728c).

Composition rules, mimicked from Spotify ((private path)):

- four or more distinct covers: a 2x2 mosaic of the first four DISTINCT covers
  in playlist order;
- one to three distinct covers (one album, a one-track list, or a few albums):
  the first cover alone;
- no cover at all, or an empty playlist: ``None``, and the route answers with
  the note-glyph placeholder.

A cover is only ever a real file the per-track ``/artwork`` chain returns
(rekordbox jpg, embedded picture, sidecar, cached online cover); nothing is
generated, and the online lookup is never triggered from here. The artist-photo
case is not built: no licensed artist image source exists yet (the Spotify
client reads playlist covers only), so one-artist lists take the rules above.

The composite is cached on disk, keyed by a hash of the size and the ordered
member ids, so a cache hit reads one file and no cover; a reorder or an edit
rebuilds it. A "no art" answer is cached for PLACEHOLDER_TTL_S only, so covers
that arrive later appear. A changed cover on an unchanged playlist keeps the
old composite until the playlist changes (accepted; see the ADR).

Requirements (mini-PRD):
  ✔︎ compose_thumbnail(): mosaic of the first four distinct covers in order.
    [if] a playlist with five distinct covers yields anything but a 2x2 grid of covers 1-4 [then ⛔️]
    [if] a repeated cover takes a mosaic slot [then ⛔️]
    [if] a playlist with no cover yields an image instead of None [then ⛔️]
  ✔︎ cached_thumbnail(): disk cache keyed by size and ordered membership.
    [if] a second call with the same members recomposes [then ⛔️]
    [if] a reordered playlist serves the old composite [then ⛔️]
    [if] an unsupported size is accepted [then ⛔️]
    [if] a cached placeholder outlives PLACEHOLDER_TTL_S [then ⛔️]
"""

from __future__ import annotations

import hashlib
import io
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from PIL import Image

ThumbnailSize = Literal[64, 232]
THUMBNAIL_SIZES: tuple[int, ...] = (64, 232)
# Bound the cover reads for a huge list: Spotify needs four distinct covers,
# and a list whose first 200 tracks show fewer than four has its answer.
MAX_TRACKS_SCANNED = 200
JPEG_QUALITY = 85
# Formats a cover may decode as; Image.open never tries any other decoder.
COVER_FORMATS = ("JPEG", "PNG", "WEBP", "GIF", "BMP")
CACHE_DIR_NAME = "playlist-thumbnails"

CoverReader = Callable[[str], bytes | None]

# Spotify's placeholder: a music-note glyph on a dark tile. A static asset,
# never a generated cover.
PLACEHOLDER_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<rect width="64" height="64" fill="#282828"/>'
    '<g fill="none" stroke="#7f7f7f" stroke-width="3">'
    '<path d="M26 44V20l18-4v24"/></g>'
    '<g fill="#7f7f7f"><circle cx="21" cy="44" r="5"/><circle cx="39" cy="40" r="5"/></g>'
    "</svg>"
)


@dataclass(frozen=True)
class Composition:
    kind: Literal["mosaic", "single", "placeholder"]
    covers: tuple[bytes, ...]


# ---------------------------------------------------------------- compose ---


def choose_covers(stable_ids: Sequence[str], read_cover: CoverReader) -> Composition:
    """Pick the covers for a playlist from its ordered members."""
    seen: set[str] = set()
    distinct: list[bytes] = []
    for stable_id in stable_ids[:MAX_TRACKS_SCANNED]:
        data = read_cover(stable_id)
        if data is None:
            continue
        digest = hashlib.sha256(data).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        distinct.append(data)
        if len(distinct) == 4:
            return Composition("mosaic", tuple(distinct))
    if distinct:
        return Composition("single", (distinct[0],))
    return Composition("placeholder", ())


def _square(data: bytes, px: int) -> Image.Image:
    image = Image.open(io.BytesIO(data), formats=COVER_FORMATS).convert("RGB")
    side = min(image.size)
    left = (image.width - side) // 2
    top = (image.height - side) // 2
    return image.crop((left, top, left + side, top + side)).resize((px, px), Image.Resampling.LANCZOS)


def render(composition: Composition, size: int) -> bytes | None:
    """JPEG bytes for a composition, or None for the placeholder case."""
    if composition.kind == "placeholder":
        return None
    canvas = Image.new("RGB", (size, size))
    if composition.kind == "single":
        canvas.paste(_square(composition.covers[0], size), (0, 0))
    elif composition.kind == "mosaic":
        half = size // 2
        for index, data in enumerate(composition.covers):
            canvas.paste(_square(data, half), ((index % 2) * half, (index // 2) * half))
    out = io.BytesIO()
    canvas.save(out, format="JPEG", quality=JPEG_QUALITY)
    return out.getvalue()


# ------------------------------------------------------------------ cache ---

# A cached "no art" answer is re-checked after this long, so covers that arrive
# later (enrichment, an accepted suggestion) show up without an edit.
PLACEHOLDER_TTL_S = 3600.0


def cache_key(size: int, stable_ids: Sequence[str]) -> str:
    digest = hashlib.sha256(f"v1|{size}|".encode())
    digest.update("\n".join(stable_ids[:MAX_TRACKS_SCANNED]).encode())
    return digest.hexdigest()


def cached_thumbnail(
    cache_dir: Path, size: int, stable_ids: Sequence[str], read_cover: CoverReader,
    *, now: Callable[[], float] = time.time,
) -> tuple[bytes | None, str]:
    """(jpeg bytes or None for the placeholder, cache key).

    A hit reads one file and no cover; only a miss reads covers and composes.
    """
    if size not in THUMBNAIL_SIZES:
        raise ValueError(f"size must be one of {THUMBNAIL_SIZES}, got {size}")
    key = cache_key(size, stable_ids)
    jpg = cache_dir / f"{key}.jpg"
    none_marker = cache_dir / f"{key}.none"
    if jpg.is_file():
        return jpg.read_bytes(), key
    if none_marker.is_file() and now() - none_marker.stat().st_mtime < PLACEHOLDER_TTL_S:
        return None, key
    data = render(choose_covers(stable_ids, read_cover), size)
    cache_dir.mkdir(parents=True, exist_ok=True)
    if data is None:
        none_marker.touch()
        return None, key
    # A unique temp file per writer: concurrent misses on one key each
    # write their own file, and the last atomic replace wins with a whole image.
    with tempfile.NamedTemporaryFile(dir=cache_dir, prefix=f"{key}.", suffix=".tmp", delete=False) as tmp:
        tmp.write(data)
    Path(tmp.name).replace(jpg)
    none_marker.unlink(missing_ok=True)
    return data, key


# ------------------------------------------------------------ track covers ---


def track_cover_bytes(stable_id: str) -> bytes | None:
    """The bytes ``GET /tracks/{id}/artwork?size=m`` would serve, never online.

    None when the chain finds no artwork; every other failure propagates.
    """
    from . import rb_vendor

    def _code(exc: HTTPException) -> str | None:
        return exc.detail.get("code") if isinstance(exc.detail, dict) else None

    try:
        content = rb_vendor.resolve_content(stable_id)
        return Path(rb_vendor.artwork_file(content, "m")).read_bytes()
    except HTTPException as exc:
        if _code(exc) not in ("VENDOR_MAPPING_NOT_FOUND", "ARTWORK_NOT_FOUND"):
            raise
    try:
        data, _mime = rb_vendor.local_artwork(stable_id, online=False)
    except HTTPException as exc:
        if exc.status_code == 404:
            return None
        raise
    return data
