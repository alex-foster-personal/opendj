"""Backend registry.  Backends call :func:`register` at import-time."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import AnalyzerBackend

BACKENDS: dict[str, "type[AnalyzerBackend]"] = {}


def register(name: str, cls: "type[AnalyzerBackend]") -> None:
    """Idempotent registration."""
    BACKENDS[name] = cls


def get_backend(name: str) -> "type[AnalyzerBackend]":
    """Lazy lookup.  Imports the named backend module if not yet loaded."""
    if name not in BACKENDS:
        # Lazy-import each independently so a preload of one backend does
        # not block the other.
        if name == "librosa+madmom":
            from . import librosa_madmom
        elif name == "mik":
            from . import mik
        else:
            from . import librosa_madmom  # noqa: F401
            from . import mik  # noqa: F401
    try:
        return BACKENDS[name]
    except KeyError as exc:
        avail = ", ".join(sorted(BACKENDS)) or "<none>"
        raise KeyError(
            f"Unknown analyser backend {name!r}; available: {avail}"
        ) from exc


__all__ = ["BACKENDS", "register", "get_backend"]
