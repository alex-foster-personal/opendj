"""Build a per-track :class:`TagSources` matrix.

Mostly a thin integration shim. The real RB / djay / MIK readers live in
``apps.shared.rekordbox_db``, ``apps.shared.djay_db``, and (future)
``apps.shared.state.mik``; this module just glues them together and
supplies ``None`` for missing sources.

Phase 7 ships the ``file`` + ``filename`` paths end-to-end. RB / djay /
MIK paths accept optional ``fetch_*`` callables so callers (and tests)
can inject their own read-through without dragging in heavy deps.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Optional

from apps.shared.tag_writer import TagRead, read_tags
from .unify import TagSources


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


def collect_for(
    path: Path,
    *,
    fetch_rb: Optional[Callable[[Path], TagRead | None]] = None,
    fetch_djay: Optional[Callable[[Path], TagRead | None]] = None,
    fetch_mik: Optional[Callable[[Path], TagRead | None]] = None,
) -> TagSources:
    """Build a :class:`TagSources` for ``path``.

    ``fetch_*`` callables let callers inject custom readers; each one is
    called with the absolute file path and must return a :class:`TagRead`
    or ``None``. Defaults: file via ``read_tags``; filename via
    :func:`parse_filename`; RB/djay/MIK None if no callable given.
    """
    file_tags: TagRead | None
    try:
        file_tags = read_tags(path)
    except Exception:
        file_tags = None

    filename_tags = parse_filename(path)
    rb = fetch_rb(path) if fetch_rb is not None else None
    djay = fetch_djay(path) if fetch_djay is not None else None
    mik = fetch_mik(path) if fetch_mik is not None else None
    return TagSources(rb=rb, mik=mik, djay=djay, file=file_tags, filename=filename_tags)


__all__ = ["collect_for", "parse_filename"]
