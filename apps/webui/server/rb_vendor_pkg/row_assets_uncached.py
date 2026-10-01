"""Row decoding that degrades instead of failing, and the uncached row path (LIBM-137).

Two things ``row_assets.py`` needs and has no room for:

* DECODING THAT CANNOT FAIL A PAGE. The ANLZ readers raise on a malformed file
  on purpose (``anlz.read_pvdi``), which is right for the one-track ``/anlz``
  route and wrong for a listing: one zero-byte ``.2EX`` would fail every page
  that contains its row, forever. :func:`decoded_preview` and
  :func:`decoded_vocals` turn a malformed file into "this source has nothing",
  logged with the track's stable id and the error's type. Never the message:
  the readers put the file's path in it.
* THE UNCACHED PATH, for a row ``row_assets`` cannot key on its own vendor
  strings: every row where the descriptor walk is unsupported (Windows), a
  ``/PIONEER/`` path with an empty, ``.`` or ``..`` component or a trailing
  dot, and any path outside ``/PIONEER/``. It resolves through the caller's
  per-call :class:`AssetResolver` and then opens the resolved path, so a swap
  between the two is not excluded. That is tolerable for one answer and wrong
  for a remembered one, so this path neither reads nor fills any memory: not
  the row cache, and not the path-keyed ``MtimeCache`` pair the one-track
  routes use.
"""

from __future__ import annotations

import logging
import struct
from pathlib import Path
from typing import Any

from apps.adapters.rekordbox import config
from apps.adapters.rekordbox import paths as rb_paths
from apps.adapters.rekordbox.models import RbRowMeta
from apps.shared.platform_paths import AssetResolver, MappedPath

from . import anlz

log = logging.getLogger(__name__)

#: Largest analysis file read into memory. The largest found in three real
#: export trees (81,075 files, Thu 1 Oct 2026) was 3,761,017 bytes; this is
#: four times that.
MAX_ASSET_BYTES: int = 16 * 1024 * 1024

#: What a malformed ANLZ file raises out of the readers: a bad magic, section
#: length or fixed header (ValueError, also numpy's for a short buffer), a
#: buffer too short to unpack (struct.error), an index past the end.
MALFORMED: tuple[type[Exception], ...] = (ValueError, struct.error, IndexError)
#: What a vendor string that cannot name a file raises out of an open or a
#: stat: OSError, or ValueError for a NUL or an unencodable character.
UNOPENABLE: tuple[type[Exception], ...] = (OSError, ValueError)

NOT_ANALYZED: dict[str, Any] = {"status": "not_analyzed"}


# ----- decoding that cannot fail a page ----------------------------------------


def _log_unreadable(stable_id: str, what: str, exc: Exception) -> None:
    log.warning("row assets: track %s has an unreadable %s (%s)", stable_id, what, type(exc).__name__)


def decoded_preview(
    reader: Any, source: anlz.BytesSource, suffix: str, stable_id: str
) -> tuple[str, int] | None:
    """``(preview_b64, preview_max)`` from one source of the chain; None when it
    carries no preview tag or cannot be decoded."""
    try:
        cols = reader(source)
        return None if cols is None else anlz.encode_preview_strip(cols)
    except MALFORMED as exc:
        _log_unreadable(stable_id, f"{suffix} preview", exc)
        return None


def decoded_vocals(source: anlz.BytesSource, stable_id: str) -> dict[str, Any]:
    """The PVDI vocals payload of a .2EX; ``not_analyzed`` when it cannot be decoded."""
    try:
        return anlz._decode_vocals_payload(source)
    except MALFORMED as exc:
        _log_unreadable(stable_id, ".2EX vocal envelope", exc)
        return dict(NOT_ANALYZED)


# ----- the uncached path ------------------------------------------------------


class _Bytes:
    """Bytes read once, shaped like the path the readers take. No ``__str__``
    naming the file: a reader's error message is never logged."""

    def __init__(self, data: bytes) -> None:
        self._data: bytes = data

    def read_bytes(self) -> bytes:
        return self._data


def _sibling_bytes(mapped: MappedPath, suffix: str, resolver: AssetResolver | None) -> bytes | None:
    """The bytes of one sibling of the analysis path; None when it is refused,
    absent, not a regular file, too large, or unreadable."""
    assert mapped.resolved is not None
    derived = rb_paths._asset_sibling(mapped, mapped.resolved.with_suffix(suffix), resolver=resolver)
    source: Path | None = derived.resolved
    if source is None or not source.is_file() or source.stat().st_size > MAX_ASSET_BYTES:
        return None
    return source.read_bytes()


def _uncached_analysis(
    analysis_data_path: str | None, resolver: AssetResolver | None, stable_id: str
) -> tuple[str | None, int | None, dict[str, Any]]:
    if not analysis_data_path:
        return None, None, dict(NOT_ANALYZED)
    mapped = rb_paths.resolve_asset_path(analysis_data_path, resolver=resolver)
    if mapped.resolved is None:
        return None, None, dict(NOT_ANALYZED)
    preview: tuple[str | None, int | None] = (None, None)
    vocals = dict(NOT_ANALYZED)
    for index, (suffix, reader) in enumerate(anlz._PREVIEW_SOURCES):
        try:
            data = _sibling_bytes(mapped, suffix, resolver)
        except UNOPENABLE:
            continue
        if data is None:
            continue
        if index == 0:  # the .2EX carries the vocal envelope as well
            vocals = decoded_vocals(_Bytes(data), stable_id)
        strip = decoded_preview(reader, _Bytes(data), suffix, stable_id)
        if strip is not None:
            preview = strip
            break
    return (*preview, vocals)


def pvdi_vocals(
    analysis_data_path: str | None, *, resolver: AssetResolver | None = None
) -> dict[str, Any]:
    """The PVDI ``vocals`` payload from a row's .2EX, ``not_analyzed`` without one.

    The one-track routes' reader: path-keyed and remembered by ``anlz.vocals_payload``,
    and a malformed file raises. The listing does not come through here.
    """
    if analysis_data_path:
        mapped = rb_paths.resolve_asset_path(analysis_data_path, resolver=resolver)
        if mapped.resolved is not None:
            twoex = rb_paths._asset_sibling(
                mapped, mapped.resolved.with_suffix(".2EX"), resolver=resolver
            )
            if twoex.resolved is not None:
                return anlz.vocals_payload(twoex.resolved)
    return {"status": "not_analyzed"}


def rb_artwork_facts(
    meta: RbRowMeta, *, resolver: AssetResolver | None = None
) -> tuple[bool | None, str]:
    """Wire pair for a rekordbox-mapped row: its pre-rendered small cover.

    The cover itself goes through the containment walk, as the artwork route's
    own lookup does, so a cover that is a symlink reads as unresolved here too.
    """
    if meta.image_path is None:
        return False, "no_image_path"
    mapped = rb_paths.resolve_asset_path(meta.image_path, resolver=resolver)
    if mapped.resolved is None:
        return False, "unresolved"
    small = rb_paths._asset_sibling(
        mapped, mapped.resolved.parent / config.ARTWORK_FILENAMES["s"], resolver=resolver
    )
    if small.resolved is None:
        return False, "unresolved"
    if not small.resolved.is_file():
        return False, "file_missing"
    return True, "ok"


def uncached_row(
    meta: RbRowMeta, resolver: AssetResolver, stable_id: str
) -> tuple[str | None, int | None, bool | None, str, dict[str, Any]]:
    """One row read by path: nothing consulted, nothing remembered, and a
    vendor string that cannot name a file is a row without assets."""
    try:
        preview_b64, preview_max, vocals = _uncached_analysis(
            meta.analysis_data_path, resolver, stable_id
        )
    except UNOPENABLE as exc:
        _log_unreadable(stable_id, "analysis path", exc)
        preview_b64, preview_max, vocals = None, None, dict(NOT_ANALYZED)
    try:
        artwork_available, artwork_status = rb_artwork_facts(meta, resolver=resolver)
    except UNOPENABLE as exc:
        _log_unreadable(stable_id, "artwork path", exc)
        artwork_available, artwork_status = False, "unresolved"
    return preview_b64, preview_max, artwork_available, artwork_status, vocals
