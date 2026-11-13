"""What the store shows a person about a plugin: the listing (PLUG-6522k).

WHY IT LIVES IN THE SIGNED INDEX, NOT THE ARCHIVE. ``opendj-plugin.json``
inside the archive is what the loader needs. The listing is what a person
reads before installing, so it must be readable without downloading the
archive, and the in-app store and a future website mirror read the same
signed copy. Text and screenshots can be fixed by re-signing the index,
without re-signing an archive.

PROBLEM FIRST (the maintainer, Tue 13 Oct 2026). A listing leads with the problem the
module solves, then what it lets you do, then how it works and its limits:

* ``tagline``: one line, the outcome;
* ``problem``: why you would want this, in the words of someone who has the
  problem;
* ``you_can``: the concrete things it lets you do, one per bullet;
* ``how_it_works``: plain text, enough to trust it;
* ``limits``: what it does not do yet, said plainly;
* ``status``: stable, beta, experiment or deprecated;
* ``screenshots``: at least one, each pinned by sha256 and size and served
  next to the index under ``media/``.

A screenshot is verified against its pinned sha256 before the app shows it,
so whoever hosts the index cannot swap an image. PNG and WebP only; SVG can
carry script and is refused.

Requirements (mini-PRD):
  * ✔︎ ✅ 🎯 a listing without a problem, a "you can" bullet or a screenshot
    is refused, naming the field. [if] a store entry can ship with no
    screenshot [then ⛔️]
  * ✔︎ ✅ 🎯 a screenshot path outside ``media/<plugin id>/`` or of another
    type is refused. [if] a listing can point the app at ``../`` or an SVG
    [then ⛔️]
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from typing import Any

from apps.plugins.manifest import CAPABILITY_KINDS, is_sha256
from apps.plugins.providers import STABLE_API_KINDS, store_surface

LISTING_STATUSES: tuple[str, ...] = ("stable", "beta", "experiment", "deprecated")
SCREENSHOT_THEMES: tuple[str, ...] = ("dark", "light", "any")
#: Per image. A store page loads several, and the index pins each one.
MAX_SCREENSHOT_BYTES: int = 1024 * 1024
MAX_SCREENSHOTS: int = 8
#: Most pixels a screenshot may have (4096 x 4096). A small, highly compressed
#: file can decode to far more; the packaging tool decodes every screenshot.
MAX_SCREENSHOT_PIXELS: int = 4096 * 4096
MEDIA_TYPES: dict[str, str] = {"png": "image/png", "webp": "image/webp"}

_LISTING_REQUIRED = frozenset({"tagline", "problem", "you_can", "how_it_works", "limits", "status", "screenshots"})
_LISTING_OPTIONAL = frozenset({"capability_kind", "provider_owner", "official_docs_url", "allowed_use_review"})
_LISTING_KEYS = _LISTING_REQUIRED | _LISTING_OPTIONAL
_SHOT_KEYS = frozenset({"path", "sha256", "size", "width", "height", "alt", "theme"})


class ListingError(ValueError):
    """The listing breaks a store rule; the message names the field."""


@dataclass(frozen=True)
class Screenshot:
    path: str  # relative to the index base, media/<plugin id>/<sha256>.<ext>
    sha256: str
    size: int
    width: int
    height: int
    alt: str
    theme: str

    @property
    def media_type(self) -> str:
        return MEDIA_TYPES[self.path.rsplit(".", 1)[1]]

    def to_wire(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "width": self.width,
            "height": self.height,
            "alt": self.alt,
            "theme": self.theme,
        }


@dataclass(frozen=True)
class Listing:
    tagline: str
    problem: str
    you_can: tuple[str, ...]
    how_it_works: str
    limits: tuple[str, ...]
    status: str
    screenshots: tuple[Screenshot, ...]
    capability_kind: str | None = None
    provider_owner: str | None = None
    official_docs_url: str | None = None
    allowed_use_review: str | None = None

    @property
    def store_surface(self) -> str | None:
        if self.capability_kind is None:
            return None
        return store_surface(self.capability_kind)

    def to_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "tagline": self.tagline,
            "problem": self.problem,
            "you_can": list(self.you_can),
            "how_it_works": self.how_it_works,
            "limits": list(self.limits),
            "status": self.status,
            "screenshots": [s.to_wire() for s in self.screenshots],
        }
        if self.capability_kind is not None:
            out["capability_kind"] = self.capability_kind
            out["store_surface"] = self.store_surface
        for key in ("provider_owner", "official_docs_url", "allowed_use_review"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out


def _text(raw: dict[str, Any], key: str, max_len: int) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ListingError(f"listing {key!r} must be non-blank text")
    if len(value) > max_len:
        raise ListingError(f"listing {key!r} is {len(value)} characters; the limit is {max_len}")
    return value.strip()


def _bullets(raw: dict[str, Any], key: str, *, least: int, most: int, max_len: int) -> tuple[str, ...]:
    value = raw.get(key)
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ListingError(f"listing {key!r} must be a list of non-blank text")
    if not least <= len(value) <= most:
        raise ListingError(f"listing {key!r} needs {least} to {most} entries, has {len(value)}")
    for item in value:
        if len(item) > max_len:
            raise ListingError(f"a listing {key!r} entry is over {max_len} characters")
    return tuple(v.strip() for v in value)


def _positive_int(raw: dict[str, Any], key: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ListingError(f"screenshot {key!r} must be a positive integer")
    return value


def _screenshot(raw: Any, plugin_id: str) -> Screenshot:
    if not isinstance(raw, dict) or set(raw) != _SHOT_KEYS:
        raise ListingError(f"a screenshot needs exactly the keys {sorted(_SHOT_KEYS)}")
    sha = raw["sha256"]
    if not is_sha256(sha):
        raise ListingError("screenshot 'sha256' must be a lowercase sha256 hex digest")
    path = raw["path"]
    allowed = {f"media/{plugin_id}/{sha}.{ext}" for ext in MEDIA_TYPES}
    if path not in allowed:
        raise ListingError(f"screenshot path {path!r} must be media/{plugin_id}/<sha256>.png or .webp")
    size = _positive_int(raw, "size")
    if size > MAX_SCREENSHOT_BYTES:
        raise ListingError(f"screenshot {path} is {size} bytes; the limit is {MAX_SCREENSHOT_BYTES}")
    theme = raw["theme"]
    if theme not in SCREENSHOT_THEMES:
        raise ListingError(f"screenshot 'theme' must be one of {list(SCREENSHOT_THEMES)}")
    return Screenshot(
        path=path,
        sha256=sha,
        size=size,
        width=_positive_int(raw, "width"),
        height=_positive_int(raw, "height"),
        alt=_text(raw, "alt", 200),
        theme=theme,
    )


def listing_from_dict(raw: Any, plugin_id: str) -> Listing:
    """Parse strictly; every refusal names the field."""
    if not isinstance(raw, dict):
        raise ListingError("'listing' must be an object")
    unknown, missing = set(raw) - _LISTING_KEYS, _LISTING_REQUIRED - set(raw)
    if unknown or missing:
        raise ListingError(f"listing keys wrong: unknown {sorted(unknown)}, missing {sorted(missing)}")
    status = raw["status"]
    if status not in LISTING_STATUSES:
        raise ListingError(f"listing 'status' must be one of {list(LISTING_STATUSES)}")
    shots = raw["screenshots"]
    if not isinstance(shots, list) or not 1 <= len(shots) <= MAX_SCREENSHOTS:
        raise ListingError(f"a listing needs 1 to {MAX_SCREENSHOTS} screenshots")
    capability_kind = raw.get("capability_kind")
    if capability_kind is not None:
        if not isinstance(capability_kind, str) or capability_kind not in CAPABILITY_KINDS:
            raise ListingError(f"listing 'capability_kind' must be one of {list(CAPABILITY_KINDS)}")
    provider_owner = _optional_text(raw, "provider_owner", 120)
    official_docs_url = _optional_https_url(raw, "official_docs_url")
    allowed_use_review = _optional_text(raw, "allowed_use_review", 2000)
    if capability_kind in STABLE_API_KINDS:
        for key, value in (
            ("provider_owner", provider_owner),
            ("official_docs_url", official_docs_url),
            ("allowed_use_review", allowed_use_review),
        ):
            if value is None:
                raise ListingError(f"listing {key!r} is required when capability_kind is {capability_kind!r}")
    return Listing(
        tagline=_text(raw, "tagline", 80),
        problem=_text(raw, "problem", 1500),
        you_can=_bullets(raw, "you_can", least=1, most=10, max_len=240),
        how_it_works=_text(raw, "how_it_works", 2000),
        limits=_bullets(raw, "limits", least=0, most=10, max_len=240),
        status=status,
        screenshots=tuple(_screenshot(s, plugin_id) for s in shots),
        capability_kind=capability_kind,
        provider_owner=provider_owner,
        official_docs_url=official_docs_url,
        allowed_use_review=allowed_use_review,
    )


def _optional_text(raw: dict[str, Any], key: str, max_len: int) -> str | None:
    if key not in raw:
        return None
    return _text(raw, key, max_len)


def _optional_https_url(raw: dict[str, Any], key: str) -> str | None:
    if key not in raw:
        return None
    value = _text(raw, key, 500)
    if not value.startswith("https://"):
        raise ListingError(f"listing {key!r} must be an https:// URL")
    return value


def sniff_media_type(data: bytes) -> str | None:
    """The type the bytes really are, or None. Never trusts the extension alone."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _png_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    """(kind, body) of each chunk up to IEND, each in bounds with a good CRC; refuses bytes after IEND."""
    pos, chunks = 8, []
    while pos < len(data):
        if pos + 12 > len(data):
            raise ListingError("the PNG ends inside a chunk header")
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind, body, end = data[pos + 4 : pos + 8], data[pos + 8 : pos + 8 + length], pos + 12 + length
        if end > len(data):
            raise ListingError(f"the PNG {kind!r} chunk runs past the end of the file")
        if zlib.crc32(kind + body) != struct.unpack(">I", data[end - 4 : end])[0]:
            raise ListingError(f"the PNG {kind!r} chunk fails its CRC")
        chunks.append((kind, body))
        pos = end
        if kind == b"IEND":
            break
    if pos != len(data):
        raise ListingError("the PNG has bytes after IEND; nothing may follow it")
    return chunks


def _check_png(data: bytes) -> None:
    """Every chunk in bounds with a good CRC, IHDR first, IEND last, and the image data inflates whole."""
    chunks = _png_chunks(data)
    kinds = [kind for kind, _ in chunks]
    if not kinds or kinds[0] != b"IHDR" or kinds[-1] != b"IEND" or b"IDAT" not in kinds:
        raise ListingError("the PNG needs IHDR first, image data, and IEND last")
    _inflate_within(b"".join(body for kind, body in chunks if kind == b"IDAT"), _png_raw_bound(chunks[0][1]))


#: Channels per PNG color type; the raw size bound depends on it.
_PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def _png_raw_bound(ihdr: bytes) -> int:
    """The most bytes the image data may inflate to, from the header's own size and pixel format."""
    if len(ihdr) != 13:
        raise ListingError("the PNG IHDR chunk must be 13 bytes")
    width, height, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", ihdr)
    if color not in _PNG_CHANNELS or depth not in (1, 2, 4, 8, 16):
        raise ListingError("the PNG IHDR names an unknown pixel format")
    _check_pixels(width, height)  # before inflating anything
    row = (width * depth * _PNG_CHANNELS[color] + 7) // 8
    # Adam7 adds at most one filter byte and one rounding byte per pass row.
    return height * (1 + row) + (4 * height + 32 if interlace else 0)


def _inflate_within(stream: bytes, bound: int) -> None:
    """Inflate in small pieces, counting, so a decompression bomb is refused before it is held in memory."""
    inflate, total, pending = zlib.decompressobj(), 0, stream
    try:
        while pending and not inflate.eof:
            total += len(inflate.decompress(pending, 1 << 16))
            if total > bound:
                raise ListingError(f"the PNG image data inflates past the {bound} bytes its size allows")
            pending = inflate.unconsumed_tail
    except zlib.error as exc:
        raise ListingError(f"the PNG image data does not inflate: {exc}") from exc
    if not inflate.eof:
        raise ListingError("the PNG image data is cut short")


def _check_webp(data: bytes) -> None:
    """The RIFF size matches the file and every chunk lies inside it."""
    if int.from_bytes(data[4:8], "little") + 8 != len(data):
        raise ListingError("the WebP RIFF size does not match the file")
    pos = 12
    while pos < len(data):
        if pos + 8 > len(data):
            raise ListingError("the WebP ends inside a chunk header")
        size = int.from_bytes(data[pos + 4 : pos + 8], "little")
        pos += 8 + size + (size & 1)
        if pos > len(data):
            raise ListingError("a WebP chunk runs past the end of the file")


def _check_pixels(width: int, height: int) -> None:
    if width * height > MAX_SCREENSHOT_PIXELS:
        raise ListingError(f"a {width}x{height} screenshot is over the {MAX_SCREENSHOT_PIXELS} pixel limit")


def image_size(data: bytes) -> tuple[int, int]:
    """(width, height) of a whole, well-formed PNG or WebP of at most ``MAX_SCREENSHOT_PIXELS``.

    Used by the packaging tool. The bytes are checked whole, not just the
    header: a truncated image would pass the client's type sniff and then fail
    to render.
    """
    width, height = _header_size(data)
    _check_pixels(width, height)
    return width, height


def _header_size(data: bytes) -> tuple[int, int]:
    kind = sniff_media_type(data)
    if kind == "image/png":
        _check_png(data)
    elif kind == "image/webp":
        _check_webp(data)
    if kind == "image/png" and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])
        return int(width), int(height)
    if kind == "image/webp":
        return _webp_size(data)
    raise ListingError("a screenshot must be a PNG or WebP image")


#: Each first WebP chunk's minimum payload and the signature bytes at an offset in it.
_WEBP_HEADERS = {b"VP8X": (10, 0, b""), b"VP8L": (5, 0, b"\x2f"), b"VP8 ": (10, 3, b"\x9d\x01\x2a")}


def _webp_size(data: bytes) -> tuple[int, int]:
    """The size a WebP's first chunk declares, refused when that chunk is too short or lacks its signature.

    A truncated header would otherwise read as empty slices, i.e. a plausible 1x1 image.
    """
    chunk = data[12:16]
    if chunk not in _WEBP_HEADERS:
        raise ListingError("a screenshot must be a PNG or WebP image")
    least, at, signature = _WEBP_HEADERS[chunk]
    body = data[20 : 20 + int.from_bytes(data[16:20], "little")]
    if len(body) < least or body[at : at + len(signature)] != signature:
        raise ListingError(f"the WebP {chunk.decode().strip()} header is truncated or damaged")
    if chunk == b"VP8X":
        return 1 + int.from_bytes(body[4:7], "little"), 1 + int.from_bytes(body[7:10], "little")
    if chunk == b"VP8L":
        bits = int.from_bytes(body[1:5], "little")
        return 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
    width, height = struct.unpack("<HH", body[6:10])
    return width & 0x3FFF, height & 0x3FFF
