"""Small cross-platform capability checks shared by test modules."""

from __future__ import annotations

from collections.abc import Callable


def posix_permission_denial_supported(
    os_name: str, geteuid: Callable[[], int] | None
) -> bool:
    """Whether chmod can create a real unreadable-directory fixture."""
    return os_name != "nt" and geteuid is not None and geteuid() != 0
