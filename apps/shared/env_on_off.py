"""Read strict on/off toggles from process environment."""

from __future__ import annotations

from collections.abc import Mapping


def read_on_off_environ(
    environ: Mapping[str, str],
    *,
    env_key: str,
    allowed: frozenset[str] | tuple[str, ...],
    default: str,
) -> bool:
    """Return True when ``env_key`` is set to ``on``; fail fast otherwise."""
    raw = environ.get(env_key, default)
    if raw not in allowed:
        raise ValueError(f"{env_key}={raw!r} is not valid; expected one of {list(allowed)}")
    return raw == "on"
