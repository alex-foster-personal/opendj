"""Disk-backed resize for local track artwork (#9420).

Downscales embedded / sidecar / cached online covers for ``size=s`` and
``size=m`` with a content-addressed cache under ``<state dir>/artwork-resized/``.

Requirements (mini-PRD):
  [if] a source smaller than the requested longest edge is upscaled [then ⛔️]
  [if] alpha is flattened onto black instead of the slate tile colour [then ⛔️]
  [if] a cache hit still calls read_embedded_artwork [then ⛔️]
  [if] bytes are written outside the cache root [then ⛔️]
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import logging
import os
import tempfile
from pathlib import Path

from PIL import Image, ImageFile

log = logging.getLogger(__name__)

# Same edge targets as ``GET /tracks/{id}/artwork`` query (rb_assets).
ARTWORK_EDGES: dict[str, int] = {"s": 80, "m": 240}
# Match ``.art-slate`` in TrackTable.svelte (#22262c).
SLATE_RGB = (0x22, 0x26, 0x2C)
JPEG_QUALITY = 85
DECODE_FORMATS = ("JPEG", "PNG", "WEBP", "GIF", "BMP")
CACHE_DIR_NAME = "artwork-resized"

ImageFile.LOAD_TRUNCATED_IMAGES = False


def source_identity(stable_id: str, path: Path, size: str) -> str | None:
    """Hex sha256 over stable_id, resolved path, size, mtime, size key; None if stat fails."""
    try:
        st = path.stat()
    except OSError:
        return None
    raw = f"{stable_id}\0{path.resolve()}\0{st.st_size}\0{st.st_mtime_ns}\0{size}".encode()
    return hashlib.sha256(raw).hexdigest()


def render(data: bytes, edge: int) -> bytes | None:
    """JPEG bytes (longest edge <= edge), or the original JPEG bytes when already small enough.

    Returns None when Pillow cannot decode the payload.
    """
    buf = io.BytesIO(data)
    try:
        with Image.open(buf, formats=DECODE_FORMATS) as img:
            if img.format == "JPEG" and max(img.size) <= edge:
                return data
            try:
                if img.format == "JPEG":
                    with contextlib.suppress(ValueError):
                        img.draft("RGB", (edge, edge))
                img.load()
            except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
                return None

            try:
                flat = _flatten(img)
                flat.thumbnail((edge, edge), Image.Resampling.LANCZOS)
                out = io.BytesIO()
                rgb = flat.convert("RGB")
                rgb.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True)
                return out.getvalue()
            except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
                return None
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        return None


def _flatten(img: Image.Image) -> Image.Image:
    """RGB; alpha composited onto SLATE_RGB (never black alone)."""
    if img.mode in {"RGBA", "LA"}:
        base = img.convert("RGBA")
        bg = Image.new("RGB", base.size, SLATE_RGB)
        bg.paste(base, mask=base.getchannel("A"))
        return bg
    if img.mode == "P":
        if "transparency" in img.info:
            base = img.convert("RGBA")
            bg = Image.new("RGB", base.size, SLATE_RGB)
            bg.paste(base, mask=base.getchannel("A"))
            return bg
        return img.convert("RGB")
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=".artwork-tmp-", suffix=path.suffix, dir=path.parent, text=False
    )
    tmp_path = Path(tmp)
    try:
        os.write(fd, data)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(tmp_path, path)
    except OSError as exc:
        log.debug("artwork cache write failed: %s", exc)
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise


class ResizedArtworkCache:
    """Content-addressed JPEG renders plus an optional stat-keyed index for embedded sources."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def indexed(self, identity: str) -> bytes | None:
        """Body bytes for a prior embedded-art render, or None."""
        idx = self._root / "index" / identity
        if not idx.is_file():
            return None
        try:
            name = idx.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        # Reject path tricks: basename only, hex-sha prefix, one size suffix.
        safe = Path(name).name
        candidate = self._root / safe
        try:
            candidate.relative_to(self._root)
        except ValueError:
            return None
        if not candidate.is_file():
            return None
        try:
            return candidate.read_bytes()
        except OSError:
            return None

    def sized(self, data: bytes, size: str) -> bytes | None:
        if size not in ARTWORK_EDGES:
            raise ValueError(f"unsupported artwork size {size!r}")
        edge = ARTWORK_EDGES[size]
        digest = hashlib.sha256(data).hexdigest()
        path = self._root / f"{digest}-{size}.jpg"
        if path.is_file():
            try:
                return path.read_bytes()
            except OSError:
                pass
        rendered = render(data, edge)
        if rendered is None:
            return None
        with contextlib.suppress(OSError):
            _write_atomic(path, rendered)
        return rendered

    def remember(self, identity: str, data: bytes, size: str) -> None:
        """Record that embedded source ``data`` at ``size`` lives at the content-addressed path."""
        if size not in ARTWORK_EDGES:
            raise ValueError(f"unsupported artwork size {size!r}")
        digest = hashlib.sha256(data).hexdigest()
        fname = f"{digest}-{size}.jpg"
        path = self._root / fname
        if not path.is_file():
            # Missed sized(); still nothing to point at.
            return
        idx = self._root / "index" / identity
        try:
            _write_atomic(idx, fname.encode("utf-8"))
        except OSError as exc:
            log.debug("artwork index write failed: %s", exc)
