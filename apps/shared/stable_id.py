"""Thin re-export shim for ``stable_id``.

Phases 7 (dedup) and 10 (USB sync) both key canonical tracks by
``stable_id``. The actual implementation lives in
``apps.shared.state.ids`` (Phase 5 shared-state layer). This module
provides a stable import path so callers never reach across into Phase 5
internals directly; if Phase 5 relocates the module later, only this
file needs updating.

Also offers two tiny convenience wrappers:

* :func:`stable_id_str` -- drops the ``(digest, tier)`` tuple and
  returns only the hex digest. Useful when callers just want a key.
* :func:`stable_id_for` -- legacy alias equivalent to :func:`stable_id`.
  Kept for the Plan 07-01 Step 3 spec surface.
"""
from __future__ import annotations

import re
from typing import Final

from apps.shared.state.ids import (
    ISRC_PATTERN,
    Tier,
    normalise_isrc,
    stable_id,
)


def stable_id_for(
    *,
    isrc: str | None,
    fingerprint: str | None = None,
    duration_ms: int | None = None,
    size_bytes: int | None = None,
    abs_path: str | None = None,
    mtime: float | None = None,
) -> tuple[str, Tier]:
    """Alias for :func:`stable_id`. Preserved for Phase 7 Plan-01 Step 3."""
    return stable_id(
        isrc=isrc,
        fingerprint=fingerprint,
        duration_ms=duration_ms,
        size_bytes=size_bytes,
        abs_path=abs_path,
        mtime=mtime,
    )


def stable_id_str(
    *,
    isrc: str | None,
    fingerprint: str | None = None,
    duration_ms: int | None = None,
    size_bytes: int | None = None,
    abs_path: str | None = None,
    mtime: float | None = None,
) -> str:
    """Return only the hex digest from :func:`stable_id`."""
    digest, _tier = stable_id(
        isrc=isrc,
        fingerprint=fingerprint,
        duration_ms=duration_ms,
        size_bytes=size_bytes,
        abs_path=abs_path,
        mtime=mtime,
    )
    return digest


def is_safe_stable_id_segment(stable_id: str) -> bool:
    """Is this id safe to use as ONE path segment under an artifact root?

    Every artifact store here is a directory per stable_id, so an id that is
    ``..``, absolute, or carries a separator selects a directory nobody meant
    to name. This is the single definition of "safe" for that question.

    It lives in ``apps.shared`` rather than beside any one store because more
    than one package needs it and ``apps.shared`` is the only package they can
    all import without a cycle. A PREDICATE, not a raiser: each caller owns
    the error type its own layer reports (an HTTP refusal, an artifact error,
    a payload error), and none of them should have to import another's.
    """
    return bool(_SAFE_SEGMENT_RE.fullmatch(stable_id)) and stable_id not in {
        ".",
        "..",
    }


_SAFE_SEGMENT_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


__all__ = [
    "ISRC_PATTERN",
    "Tier",
    "is_safe_stable_id_segment",
    "normalise_isrc",
    "stable_id",
    "stable_id_for",
    "stable_id_str",
]
