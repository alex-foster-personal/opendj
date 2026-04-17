"""``stable_id`` algorithm -- open-dj v0 strawman, section 5.

Three-tier identifier:

1. **ISRC** -- if a normalisable ISRC is present, the ID is
   ``sha1(normalised_isrc)``, tagged ``'isrc'``. Two tracks with the same
   ISRC collide by design; that is the archival property.
2. **Fingerprint** -- ``sha1(fp[:64] + '|' + duration_ms + '|' + size_bytes)``,
   tagged ``'fingerprint'``.
3. **Inferred** -- ``sha1(abs_path + '|' + mtime)``, tagged ``'inferred'``.
   Last resort. Phase 7 dedup re-keys these when better signals exist.

The output is always a 40-char lowercase hex SHA-1 string.
"""
from __future__ import annotations

import hashlib
import re
from typing import Literal

ISRC_PATTERN: re.Pattern[str] = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}[0-9]{7}$")

Tier = Literal["isrc", "fingerprint", "inferred"]


def normalise_isrc(raw: str | None) -> str | None:
    """Normalise ``raw`` to canonical ISRC form, or None if invalid.

    Strips non-alphanumeric characters, uppercases, and validates against
    :data:`ISRC_PATTERN`. Returns None for empty / malformed input.
    """
    if raw is None:
        return None
    cleaned = "".join(c for c in raw if c.isalnum()).upper()
    if not cleaned:
        return None
    return cleaned if ISRC_PATTERN.match(cleaned) else None


def stable_id(
    *,
    isrc: str | None,
    fingerprint: str | None = None,
    duration_ms: int | None = None,
    size_bytes: int | None = None,
    abs_path: str | None = None,
    mtime: float | None = None,
) -> tuple[str, Tier]:
    """Return ``(sha1_hex, tier)`` per the open-dj v0 strawman, section 5.

    Falls through the three tiers in order. Raises :class:`ValueError` only
    when no tier can produce a result -- i.e. no ISRC, no fingerprint
    triple, and no path. Callers MAY pass ``abs_path=""`` and ``mtime=0.0``
    to force tier-3 output for streaming-only entries (the collision is
    accepted by the identifier policy).
    """
    normalised = normalise_isrc(isrc)
    if normalised is not None:
        digest = hashlib.sha1(normalised.encode("ascii")).hexdigest()
        return digest, "isrc"

    if (
        fingerprint is not None
        and fingerprint != ""
        and duration_ms is not None
        and size_bytes is not None
    ):
        material = "|".join(
            [fingerprint[:64], str(duration_ms), str(size_bytes)]
        )
        digest = hashlib.sha1(material.encode("utf-8")).hexdigest()
        return digest, "fingerprint"

    if abs_path is None and mtime is None:
        raise ValueError(
            "stable_id requires ISRC, a (fingerprint, duration, size) triple, "
            "or an abs_path+mtime pair; none were provided."
        )
    path_str = abs_path if abs_path is not None else ""
    mtime_val = mtime if mtime is not None else 0.0
    material = f"{path_str}|{mtime_val}"
    digest = hashlib.sha1(material.encode("utf-8")).hexdigest()
    return digest, "inferred"


__all__ = ["ISRC_PATTERN", "Tier", "normalise_isrc", "stable_id"]
