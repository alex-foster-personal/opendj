"""Build a per-track :class:`TagSources` matrix.

Thin integration shim. The real RB / djay / MIK readers live in
``apps.shared.rekordbox_db``, ``apps.shared.djay_db``, and (when Phase 6
ships MIK rows) ``apps.shared.state``; this module glues them together
and supplies ``None`` for sources that are not available.

Phase 7 ships the ``file`` + ``filename`` paths end-to-end. This
gap-fill pass also wires real defaults for ``fetch_rb`` + ``fetch_djay``
+ ``fetch_mik``: the defaults try to read from the on-disk vendor DBs
and return ``None`` when a DB is unavailable or the track is not
indexed. Callers can still inject their own callables for tests or when
they already hold a reader open.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

from apps.shared.tag_writer import TagRead, read_tags

from ._helpers import vendor_path_key
from .unify import TagSources

_LOG = logging.getLogger(__name__)
_FILENAME_RE = re.compile(r"^(?P<artist>[^-]+?)\s*-\s*(?P<title>.+?)(?:\s*\[.*\])?$")

_TTrack = TypeVar("_TTrack")


def parse_filename(path: Path) -> TagRead:
    """Extract artist/title from ``Artist - Title.ext`` filenames.

    Returns an otherwise-empty :class:`TagRead` with just those two
    fields populated. Unparseable names yield an empty :class:`TagRead`.
    """
    stem = path.stem
    m = _FILENAME_RE.match(stem)
    if not m:
        return TagRead()
    return TagRead(
        artist=m.group("artist").strip(),
        title=m.group("title").strip(),
    )


# ---------------------------------------------------------------- RB default


@dataclass
class _VendorIndexCache:
    """Thread-safe lazy builder for path -> :class:`TagRead` vendor indexes."""

    _lock: threading.Lock = field(default_factory=threading.Lock)
    _index: dict[str, TagRead] | None = None
    _unavailable: bool = False

    def get(self, build: Callable[[], dict[str, TagRead] | None]) -> dict[str, TagRead] | None:
        if self._unavailable:
            return None
        if self._index is not None:
            return self._index
        with self._lock:
            if self._unavailable:
                return None
            if self._index is not None:
                return self._index
            built = build()
            if built is None:
                self._unavailable = True
                return None
            self._index = built
            return built

    def reset_for_tests(self) -> None:
        with self._lock:
            self._index = None
            self._unavailable = False


_RB_VENDOR_CACHE = _VendorIndexCache()


def _build_path_index(
    tracks: list[_TTrack],
    *,
    include: Callable[[_TTrack], bool],
    path_of: Callable[[_TTrack], str | None],
    to_tag_read: Callable[[_TTrack], TagRead],
) -> dict[str, TagRead]:
    index: dict[str, TagRead] = {}
    for track in tracks:
        if not include(track):
            continue
        raw = path_of(track)
        if not raw:
            continue
        key = vendor_path_key(raw)
        if not key:
            continue
        index[key] = to_tag_read(track)
    return index


def _rb_build_index() -> dict[str, TagRead] | None:
    """Open the working-copy RB DB and build a ``{path: TagRead}`` index.

    Returns ``None`` when the DB cannot be opened (missing, live lock,
    pyrekordbox import failure, etc.). Safe in environments without RB
    installed -- the one-time failure is logged at debug level.
    """
    try:
        from apps.shared import rekordbox_db
    except Exception as exc:  # pragma: no cover -- pyrekordbox absent
        _LOG.debug("rekordbox_db import failed: %s", exc)
        return None
    try:
        db = rekordbox_db.open_db()
    except Exception as exc:
        _LOG.debug("rekordbox_db open failed: %s", exc)
        return None

    try:
        tracks = list(rekordbox_db.iter_tracks(db))
    except Exception as exc:  # pragma: no cover -- defensive
        _LOG.debug("rekordbox_db iter_tracks failed: %s", exc)
        return None
    return _build_path_index(
        tracks,
        include=lambda track: not track.is_streaming and track.file_path is not None,
        path_of=lambda track: str(track.file_path),
        to_tag_read=lambda track: TagRead(
            title=track.title or None,
            artist=track.artist or None,
            album=track.album or None,
            genre=track.genre or None,
            bpm=track.bpm,
            rating=track.rating,
            isrc=track.isrc,
        ),
    )


def _rb_index() -> dict[str, TagRead] | None:
    """Lazy + thread-safe accessor for the path -> TagRead index."""
    return _RB_VENDOR_CACHE.get(_rb_build_index)


def _lookup_in_vendor_index(
    index_getter: Callable[[], dict[str, TagRead] | None],
    path: Path,
) -> TagRead | None:
    """Look up ``path`` in a lazily built vendor index, or ``None`` if absent."""
    index = index_getter()
    if index is None:
        return None
    return index.get(vendor_path_key(str(path)))


def default_fetch_rb(path: Path) -> TagRead | None:
    """Default RB fetcher: look up the Rekordbox working-copy by path.

    Returns ``None`` when the DB is unreachable or the path is not in
    the library.
    """
    return _lookup_in_vendor_index(_rb_index, path)


# -------------------------------------------------------------- djay default


_DJAY_VENDOR_CACHE = _VendorIndexCache()


def _djay_build_index() -> dict[str, TagRead] | None:
    """Open the djay MediaLibrary.db and build a path -> TagRead index."""
    try:
        from apps.shared import djay_db
        from apps.shared import paths as shared_paths
    except Exception as exc:  # pragma: no cover
        _LOG.debug("djay_db import failed: %s", exc)
        return None

    candidate = getattr(shared_paths, "DJAY_LIVE_DB", None)
    if candidate is None or not Path(candidate).exists():
        _LOG.debug("djay live DB not found at %s", candidate)
        return None

    try:
        tracks = list(djay_db.iter_tracks(Path(candidate)))
    except Exception as exc:
        _LOG.debug("djay_db iter_tracks failed: %s", exc)
        return None

    return _build_path_index(
        tracks,
        include=lambda track: track.is_local and track.file_path is not None,
        path_of=lambda track: str(track.file_path),
        to_tag_read=lambda track: TagRead(
            title=track.title or None,
            artist=track.artist or None,
            isrc=track.isrc or None,
            rating=track.rating if track.rating is not None else None,
        ),
    )


def _djay_index() -> dict[str, TagRead] | None:
    return _DJAY_VENDOR_CACHE.get(_djay_build_index)


def default_fetch_djay(path: Path) -> TagRead | None:
    """Default djay fetcher: look up ``MediaLibrary.db`` by local path."""
    return _lookup_in_vendor_index(_djay_index, path)


# --------------------------------------------------------------- MIK default


_MIK_UNAVAILABLE_LOGGED = False


def default_fetch_mik(_path: Path) -> TagRead | None:
    """Default MIK fetcher.

    Phase 6 has not landed a dedicated MIK table in
    ``apps.shared.state.schema``; there is no ``analysis`` / ``mik`` row
    shape to read from yet. This fetcher returns ``None`` and logs a
    one-time warning. Once Phase 6 ships, swap this to query the
    ``track_fields`` table (source=``mik``) via the canonical
    ``stable_id``.
    """
    global _MIK_UNAVAILABLE_LOGGED
    if not _MIK_UNAVAILABLE_LOGGED:
        _LOG.warning(
            "MIK fetcher is not wired: Phase 6 has not shipped a MIK row shape "
            "in apps.shared.state.schema; collector will treat MIK as absent."
        )
        _MIK_UNAVAILABLE_LOGGED = True
    return None


# ------------------------------------------------------------------ helpers


def _reset_caches_for_tests() -> None:
    """Clear module-level caches. Test-only hook."""
    global _MIK_UNAVAILABLE_LOGGED
    _RB_VENDOR_CACHE.reset_for_tests()
    _DJAY_VENDOR_CACHE.reset_for_tests()
    _MIK_UNAVAILABLE_LOGGED = False


# ------------------------------------------------------------------ collect


def collect_for(
    path: Path,
    *,
    fetch_rb: Callable[[Path], TagRead | None] | None = None,
    fetch_djay: Callable[[Path], TagRead | None] | None = None,
    fetch_mik: Callable[[Path], TagRead | None] | None = None,
) -> TagSources:
    """Build a :class:`TagSources` for ``path``.

    ``fetch_*`` callables default to the real vendor readers
    (:func:`default_fetch_rb`, :func:`default_fetch_djay`,
    :func:`default_fetch_mik`). Pass an explicit callable to override
    (e.g. tests) or pass ``lambda _p: None`` to disable a source.
    """
    file_tags: TagRead | None
    try:
        file_tags = read_tags(path)
    except Exception:
        file_tags = None

    filename_tags = parse_filename(path)
    rb_fn = fetch_rb if fetch_rb is not None else default_fetch_rb
    djay_fn = fetch_djay if fetch_djay is not None else default_fetch_djay
    mik_fn = fetch_mik if fetch_mik is not None else default_fetch_mik

    try:
        rb = rb_fn(path)
    except Exception as exc:
        _LOG.debug("fetch_rb raised %s for %s", exc, path)
        rb = None
    try:
        djay = djay_fn(path)
    except Exception as exc:
        _LOG.debug("fetch_djay raised %s for %s", exc, path)
        djay = None
    try:
        mik = mik_fn(path)
    except Exception as exc:
        _LOG.debug("fetch_mik raised %s for %s", exc, path)
        mik = None

    return TagSources(rb=rb, mik=mik, djay=djay, file=file_tags, filename=filename_tags)


__all__ = [
    "collect_for",
    "default_fetch_djay",
    "default_fetch_mik",
    "default_fetch_rb",
    "parse_filename",
]
