"""Shared ``Pattern.fullmatch`` guards for caller-owned identifier grammars.

Each caller keeps its regex and error type (HTTP refusal, ``ValueError``, and
so on). This module only centralizes the match-or-refuse shape so path and
route layers do not duplicate it.
"""
from __future__ import annotations

import re
from collections.abc import Callable

__all__ = ["require_matching"]


def require_matching(
    value: str,
    pattern: re.Pattern[str],
    *,
    field: str,
    description: str,
    refuse: Callable[[str], Exception] = ValueError,
) -> str:
    """Return ``value`` when ``pattern.fullmatch(value)`` succeeds; otherwise refuse."""
    if pattern.fullmatch(value) is None:
        message = f"{field} must be {description}, got {value!r}"
        raise refuse(message)
    return value
