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


__all__ = [
    "ISRC_PATTERN",
    "Tier",
    "normalise_isrc",
    "stable_id",
    "stable_id_for",
    "stable_id_str",
]
