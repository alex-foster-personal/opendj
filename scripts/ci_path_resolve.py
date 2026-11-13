"""Shared PATH lookup for helpers that must fail by naming the missing program.

Callers use either :func:`resolve_on_path`, which raises ``SystemExit`` with the
``[ERROR] ...`` sentence CI gates expect, or :func:`require_on_path` with a
caller-supplied exception type and message (git/pnpm wrappers in scripts).
"""

from __future__ import annotations

import os
import shutil


def _without_empty_entries(path: str) -> str:
    """PATH with empty entries dropped, so none of them means the working directory."""
    return os.pathsep.join(entry for entry in path.split(os.pathsep) if entry)


def require_on_path(name: str, exc_type: type[BaseException], message: str) -> str:
    """Return the path of ``name`` on PATH, or raise ``exc_type(message)``.

    On Windows, ``shutil.which`` honors PATHEXT and can resolve ``pnpm.CMD``.
    A bare ``pnpm`` argv without ``shell=True`` goes to ``CreateProcess``, which
    appends ``.exe`` only and never consults PATHEXT, so resolving the full path
    is required for portable subprocess launches (see ``quality_gate._pnpm``).
    An empty PATH entry never resolves to a bare name in the working directory;
    a relative directory entry resolves as ``shutil.which`` reads it.
    """
    found = shutil.which(name)
    if found is not None and not os.path.dirname(found):
        # shutil.which reads an empty PATH entry as the working directory; never
        # hand back that bare relative name, look again without empty entries.
        found = shutil.which(name, path=_without_empty_entries(os.environ.get("PATH", os.defpath)))
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
