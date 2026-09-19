"""``compute_track_id`` -- open-dj v0.2 spec section 5 identity chain.

Per D2, ``track_id`` IS ``stable_id`` -- we delegate to
:mod:`apps.shared.state.ids`.
"""
from __future__ import annotations

from collections.abc import Mapping

from apps.shared.state.ids import Tier, stable_id


def compute_track_id(meta: Mapping[str, object]) -> str:
    """Return the open-dj ``track_id`` (40-char lowercase sha1 hex)."""
    return compute_track_id_with_tier(meta)[0]


def compute_track_id_with_tier(
    meta: Mapping[str, object],
) -> tuple[str, Tier]:
    """Like :func:`compute_track_id` but returns ``(hex, tier)``."""
    fp = meta.get("chromaprint_fingerprint") or meta.get("fingerprint")
    path = meta.get("absolute_path") or meta.get("file_path")
    mtime = meta.get("mtime")
    return stable_id(
        isrc=_as_str(meta.get("isrc")),
        fingerprint=_as_str(fp),
        duration_ms=_as_int(meta.get("duration_ms")),
        size_bytes=_as_int(meta.get("size_bytes")),
        abs_path=_as_str(path) if path is not None else None,
        mtime=_as_float(mtime) if mtime is not None else None,
    )


def _as_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _as_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _as_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None
