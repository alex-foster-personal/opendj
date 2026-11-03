"""Shared PATH lookup for CI gate helpers that fail by naming the missing program."""

from __future__ import annotations

import shutil


def resolve_on_path(name: str, *, without: str) -> str:
    """The absolute path of ``name`` on PATH, or SystemExit naming it and ``without``."""
    found = shutil.which(name)
    if found is None:
        raise SystemExit(f"[ERROR] {name} is not on PATH; {without} cannot run without it")
    return found
