"""Shared PATH lookup for helpers that must fail by naming the missing program.

Callers use either :func:`resolve_on_path`, which raises ``SystemExit`` with the
``[ERROR] ...`` sentence CI gates expect, or :func:`require_on_path` with a
caller-supplied exception type and message (git/pnpm wrappers in scripts).
"""

from __future__ import annotations

import shutil


def require_on_path(name: str, exc_type: type[BaseException], message: str) -> str:
    """Return the path of ``name`` on PATH, or raise ``exc_type(message)``.

    On Windows, ``shutil.which`` honors PATHEXT and can resolve ``pnpm.CMD``.
    A bare ``pnpm`` argv without ``shell=True`` goes to ``CreateProcess``, which
    appends ``.exe`` only and never consults PATHEXT, so resolving the full path
    is required for portable subprocess launches (see ``quality_gate._pnpm``).
    """
    found = shutil.which(name)
    if found is None:
        raise exc_type(message)
    return found


def resolve_on_path(name: str, *, without: str) -> str:
    """The path of ``name`` on PATH, or SystemExit naming it and ``without``."""
    return require_on_path(
        name,
        SystemExit,
        f"[ERROR] {name} is not on PATH; {without} cannot run without it",
    )
