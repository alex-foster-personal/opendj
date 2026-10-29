"""Platform seam for who the current process runs as.

AGENTS.md "Portable by default": ``os.getuid`` is POSIX-only, so callers ask
here instead of probing it themselves. Windows has no uid and no root, so the
answer there is "not root".
"""

from __future__ import annotations

import os
from collections.abc import Callable

_GETUID: Callable[[], int] | None = getattr(os, "getuid", None)


def running_as_root(getuid: Callable[[], int] | None = _GETUID) -> bool:
    """True only on POSIX under uid 0; False on Windows, which has no ``os.getuid``."""
    return getuid is not None and getuid() == 0
