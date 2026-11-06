"""Platform seam for who the current process runs as.

AGENTS.md "Portable by default": ``os.getuid`` is POSIX-only, so callers ask
here instead of probing it themselves. Windows has no uid and no root, so the
answer there is "not root". This module also names a filesystem-safe per-user
fragment for shared-temp lock files: POSIX uid, or a sanitized USERNAME / USER
token where there is no uid.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping

_GETUID: Callable[[], int] | None = getattr(os, "getuid", None)
_UNSAFE_LOCK_TOKEN = re.compile(r"[^A-Za-z0-9._-]")


def running_as_root(getuid: Callable[[], int] | None = _GETUID) -> bool:
    """True only on POSIX under uid 0; False on Windows, which has no ``os.getuid``."""
    return getuid is not None and getuid() == 0


def _sanitize_lock_token(raw: str) -> str:
    token = _UNSAFE_LOCK_TOKEN.sub("_", raw.strip()).strip("._-")
    return token or "nouser"


def lock_identity_token(
    getuid: Callable[[], int] | None = _GETUID,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Filesystem-safe per-user fragment for shared-temp lock names.

    POSIX (``getuid`` present): decimal uid, matching the previous
    ``os.getuid()`` lock suffix. Windows (``getuid`` is ``None``):
    ``USERNAME``, then ``USER``, then ``nouser``. Unsafe filename
    characters in the env values become ``_``.
    """
    if getuid is not None:
        return str(getuid())
    env = os.environ if environ is None else environ
    raw = env.get("USERNAME") or env.get("USER") or "nouser"
    return _sanitize_lock_token(raw)
