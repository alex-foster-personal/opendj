"""Home-relative path display for published records (no operator login)."""

from __future__ import annotations

from pathlib import Path


def published_source(path: Path) -> str:
    """A path under the home dir is written `~/...`; anything else is `str(path)`.

    Never record an absolute machine path: it leaks the username into the
    published tree (see OSS-PUBLIC-PUSH-CHECKLIST.md) and breaks on every
    other machine.
    """
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)
