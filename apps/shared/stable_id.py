"""Stable identifier for a track (Phase 7 dedup + open-dj v0 strawman).

Three-tier algorithm (matches ``apps.shared.state.ids.stable_id`` when
that module is present -- we implement the same algorithm here so Phase 7
can ship without depending on Phase 5's shared-state layer):

1. **ISRC tier** -- if a normalisable ISRC is present, the id is a
   16-char hex prefix of ``sha1(normalised_isrc)``. Two tracks with the
   same ISRC collide by design (that is the archival property).
2. **Fingerprint tier** -- ``sha1(fp[:64] + '|' + duration_ms + '|' +
   size_bytes)`` truncated to 16 hex. Two rips of the same track at
   different bitrates produce the same id because chromaprint's first
   64 chars are stable against re-encodes; duration + size disambiguate.
3. **Inferred tier** -- ``sha1(abs_path + '|' + mtime)`` truncated to
   16 hex. Last resort; collides on any path rename.

This module is intentionally self-contained. A future Phase 5 migration
can re-point callers at ``apps.shared.state.ids`` with no digest churn
because the algorithm is identical.
"""
from __future__ import annotations

import hashlib
import re
from typing import Literal

# Matches the open-dj v0 strawman, section 5: country (2 letters) +
# registrant (3 alphanumerics) + year+designation (5 digits) +
# designation cont. (2 digits) == 12 chars total.
ISRC_PATTERN: re.Pattern[str] = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}[0-9]{7}$")

Tier = Literal["isrc", "fingerprint", "inferred"]


def normalise_isrc(raw: str | None) -> str | None:
    """Normalise ``raw`` to canonical ISRC form, or None if invalid."""
    if raw is None:
        return None
    cleaned = "".join(c for c in raw if c.isalnum()).upper()
    if not cleaned:
        return None
    return cleaned if ISRC_PATTERN.match(cleaned) else None


def _sha1_prefix(s: str, *, length: int = 16) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:length]


def stable_id_for(
    *,
    isrc: str | None,
    fingerprint: str | None = None,
    duration_ms: int | None = None,
    size_bytes: int | None = None,
    abs_path: str | None = None,
    mtime: float | None = None,
) -> tuple[str, Tier]:
    """Return ``(digest, tier)`` per the tier ladder above.

    Raises ``ValueError`` when no tier can be satisfied.
    """
    iso = normalise_isrc(isrc)
    if iso is not None:
        return _sha1_prefix(iso), "isrc"
    if fingerprint and duration_ms is not None and size_bytes is not None:
        # Trim fingerprint to its first 64 chars for stability against
        # minor re-encodes; duration + size provide disambiguation.
        key = f"{fingerprint[:64]}|{int(duration_ms)}|{int(size_bytes)}"
        return _sha1_prefix(key), "fingerprint"
    if abs_path is not None and mtime is not None:
        key = f"{abs_path}|{float(mtime):.3f}"
        return _sha1_prefix(key), "inferred"
    raise ValueError(
        "stable_id_for: need isrc, or fingerprint+duration+size, "
        "or abs_path+mtime"
    )


def stable_id(
    *,
    isrc: str | None,
    fingerprint: str | None = None,
    duration_ms: int | None = None,
    size_bytes: int | None = None,
    abs_path: str | None = None,
    mtime: float | None = None,
) -> tuple[str, Tier]:
    """Return ``(digest, tier)`` -- alias for :func:`stable_id_for`.

    This matches the ``apps.shared.state.ids.stable_id`` signature so
    callers can swap imports without code changes.
    """
    return stable_id_for(
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
    """Return only the hex digest from :func:`stable_id_for`."""
    digest, _tier = stable_id_for(
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
