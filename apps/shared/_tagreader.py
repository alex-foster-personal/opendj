"""Single import-guard and entry point for the audio tag reader (tinytag).

Why this exists
---------------

Tag reading used to go through ``mutagen``, which is GPL-2.0-or-later and so
could only ship as an opt-in extra of this Apache-2.0 package; the desktop
payload omitted it and every tag-backed surface (embedded artwork, file
genre, upload duration) reported "reader unavailable". Since Thu 1 Oct 2026
every READ goes through ``tinytag`` (MIT, pure Python), a core dependency.
Research: ``research/tag-reading/2026-10-01-mutagen-replacement.md``.

mutagen remains only behind the opt-in ``[tags]`` extra for the tag WRITE
family (``apps.shared.tag_writer``, ``apps.analysis.write_tags``, the Serato
GEOB codec), guarded by :mod:`apps.shared._mutagen`. Nothing that reads tags
for the library, ingest, artwork or reconcile surfaces may import mutagen.

Every callsite imports from this module, so:

* Modules still import when ``tinytag`` is absent (a broken or hand-built
  environment); :data:`HAS_TAG_READER` is ``False`` and the HTTP surfaces
  answer their documented "reader unavailable" 503 rather than guessing.
* A caller that genuinely needs tags invokes :func:`require` and gets a
  clean, self-describing :class:`ImportError`.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

try:  # pragma: no cover - trivially exercised at import time
    import tinytag as _tinytag  # type: ignore
    HAS_TAG_READER: bool = True
except ImportError:  # pragma: no cover - only on environments missing the core dep
    _tinytag = None  # type: ignore[assignment]
    HAS_TAG_READER = False

if TYPE_CHECKING:  # pragma: no cover
    from tinytag import TinyTag


_INSTALL_HINT = (
    "audio tag reading requires the 'tinytag' package, a core dependency of "
    "music-dj-tools; reinstall the environment (uv sync)."
)


def require() -> None:
    """Raise :class:`ImportError` with an install hint when tinytag is absent."""
    if not HAS_TAG_READER:
        raise ImportError(_INSTALL_HINT)


class TagReadError(Exception):
    """The file could not be parsed as tagged audio (corrupt, truncated, not audio)."""


def read(path: Path | str, *, image: bool = False) -> TinyTag:
    """Parse ``path`` with tinytag; raise :class:`TagReadError` when it cannot.

    Callers decide whether a failure is data (a library walk) or an error
    (an upload probe). ``image=True`` also loads embedded pictures.
    """
    require()
    from tinytag import TinyTag  # type: ignore

    try:
        return TinyTag.get(str(path), image=image)
    except Exception as exc:
        raise TagReadError(str(exc) or type(exc).__name__) from exc


def first_other(tag: TinyTag, key: str) -> str | None:
    """First non-empty value of tinytag's ``other`` field ``key`` (lowercase)."""
    values = (tag.other or {}).get(key) or ()
    for value in values:
        text = str(value).strip()
        if text:
            return text
    return None


__all__ = ["HAS_TAG_READER", "TagReadError", "first_other", "read", "require"]
