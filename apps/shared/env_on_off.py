"""Read strict on/off toggles from process environment."""

from __future__ import annotations

from collections.abc import Mapping


def read_on_off_environ(
    environ: Mapping[str, str],
    *,
    env_key: str,
    allowed: frozenset[str] | tuple[str, ...],
    default: str,
    normalize: bool = False,
) -> bool:
    """Return True when ``env_key`` is set to ``on``; fail fast otherwise.

    ``normalize=True`` folds whitespace and case (``.strip().lower()``) before
    the enum check. The default is False (strict exact match) for callers such
    as analysis and lyric autostart.
    """
    raw = environ.get(env_key, default)
    if normalize:
        raw = raw.strip().lower()
    if raw not in allowed:
        raise ValueError(f"{env_key}={raw!r} is not valid; expected one of {list(allowed)}")
    return raw == "on"
