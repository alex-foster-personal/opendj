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
from pathlib import Path

from apps.shared.tag_writer import TagRead, read_tags

from .unify import TagSources

_LOG = logging.getLogger(__name__)
_FILENAME_RE = re.compile(r"^(?P<artist>[^-]+?)\s*-\s*(?P<title>.+?)(?:\s*\[.*\])?$")


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


_RB_CACHE_LOCK = threading.Lock()
_RB_INDEX_CACHE: dict[str, TagRead] | None = None
_RB_UNAVAILABLE: bool = False


def _rb_normalise_path(raw: str) -> str:
    """Normalise a FolderPath for indexing: expanduser + lowercase.

    Mirrors what ``apps.shared.rekordbox_db._to_path`` does for non-streaming
    rows; we only index local-file rows so streaming URIs are skipped.
    """
    if not raw:
        return ""
    try:
        return str(Path(raw).expanduser()).casefold()
    except Exception:
        return raw.casefold()


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

    index: dict[str, TagRead] = {}
    try:
        for track in rekordbox_db.iter_tracks(db):
            if track.is_streaming or track.file_path is None:
                continue
            key = _rb_normalise_path(str(track.file_path))
            if not key:
                continue
            index[key] = TagRead(
                title=track.title or None,
                artist=track.artist or None,
                album=track.album or None,
                genre=track.genre or None,
                bpm=track.bpm,
                rating=track.rating,
                isrc=track.isrc,
            )
    except Exception as exc:  # pragma: no cover -- defensive
        _LOG.debug("rekordbox_db iter_tracks failed: %s", exc)
        return None
    return index


def _rb_index() -> dict[str, TagRead] | None:
    """Lazy + thread-safe accessor for the path -> TagRead index."""
    global _RB_INDEX_CACHE, _RB_UNAVAILABLE
    if _RB_UNAVAILABLE:
        return None
    if _RB_INDEX_CACHE is not None:
        return _RB_INDEX_CACHE
    with _RB_CACHE_LOCK:
        if _RB_UNAVAILABLE:
            return None
        if _RB_INDEX_CACHE is not None:
            return _RB_INDEX_CACHE
        built = _rb_build_index()
        if built is None:
            _RB_UNAVAILABLE = True
            return None
        _RB_INDEX_CACHE = built
        return built


def default_fetch_rb(path: Path) -> TagRead | None:
    """Default RB fetcher: look up the Rekordbox working-copy by path.

    Returns ``None`` when the DB is unreachable or the path is not in
    the library.
    """
    index = _rb_index()
    if index is None:
        return None
    key = _rb_normalise_path(str(path))
    return index.get(key)


# -------------------------------------------------------------- djay default


_DJAY_CACHE_LOCK = threading.Lock()
_DJAY_INDEX_CACHE: dict[str, TagRead] | None = None
_DJAY_UNAVAILABLE: bool = False


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

    index: dict[str, TagRead] = {}
    for track in tracks:
        if not track.is_local or track.file_path is None:
            continue
        key = _rb_normalise_path(str(track.file_path))
        if not key:
            continue
        index[key] = TagRead(
            title=track.title or None,
            artist=track.artist or None,
            isrc=track.isrc or None,
            rating=track.rating if track.rating is not None else None,
        )
    return index


def _djay_index() -> dict[str, TagRead] | None:
    global _DJAY_INDEX_CACHE, _DJAY_UNAVAILABLE
    if _DJAY_UNAVAILABLE:
        return None
    if _DJAY_INDEX_CACHE is not None:
        return _DJAY_INDEX_CACHE
    with _DJAY_CACHE_LOCK:
        if _DJAY_UNAVAILABLE:
            return None
        if _DJAY_INDEX_CACHE is not None:
            return _DJAY_INDEX_CACHE
        built = _djay_build_index()
        if built is None:
            _DJAY_UNAVAILABLE = True
            return None
        _DJAY_INDEX_CACHE = built
        return built


def default_fetch_djay(path: Path) -> TagRead | None:
    """Default djay fetcher: look up ``MediaLibrary.db`` by local path."""
    index = _djay_index()
    if index is None:
        return None
    key = _rb_normalise_path(str(path))
    return index.get(key)


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
    global _RB_INDEX_CACHE, _RB_UNAVAILABLE
    global _DJAY_INDEX_CACHE, _DJAY_UNAVAILABLE
    global _MIK_UNAVAILABLE_LOGGED
    with _RB_CACHE_LOCK:
        _RB_INDEX_CACHE = None
        _RB_UNAVAILABLE = False
    with _DJAY_CACHE_LOCK:
        _DJAY_INDEX_CACHE = None
        _DJAY_UNAVAILABLE = False
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
