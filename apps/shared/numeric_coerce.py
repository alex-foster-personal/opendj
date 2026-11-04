"""Optional numeric coercion with empty/invalid → None."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

_T = TypeVar("_T")


def coerce_optional(value: Any, converter: Callable[[Any], _T]) -> _T | None:
    """Apply ``converter`` to non-empty values; return ``None`` for empty or invalid input."""
    if value is None or value == "":
        return None
    try:
        return converter(value)
    except (TypeError, ValueError):
        return None
