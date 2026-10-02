"""Private membership checks for analysis string enums. No apps.* imports."""
from __future__ import annotations

from typing import TypeVar

E = TypeVar("E", bound=BaseException)


def require_allowed_value(
    value: str,
    allowed: tuple[str, ...],
    subject: str,
    allowed_label: str,
    exc_type: type[E],
) -> str:
    if value not in allowed:
        raise exc_type(
            f"unknown {subject} {value!r}; {allowed_label} are {allowed}"
        )
    return value
