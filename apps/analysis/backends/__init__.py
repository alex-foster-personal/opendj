"""Backend registry.  Backends call :func:`register` at import-time."""
from __future__ import annotations

import importlib.util
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import AnalyzerBackend

BACKENDS: dict[str, "type[AnalyzerBackend]"] = {}
# Portable default: librosa + scipy install from PyPI wheels (`analysis`
# extra), unlike the git-HEAD-only madmom dev backend.
DEFAULT_BACKEND: str = "librosa"

#: What ``DEFAULT_BACKEND`` needs importable at runtime. Kept in step with
#: ``LibrosaBackend._require_deps``, which raises BackendNotAvailable on the
#: same names; a test asserts the two agree.
DEFAULT_BACKEND_MODULES: tuple[str, ...] = ("librosa", "scipy")


def default_backend_installed() -> bool:
    """True when the drain's default backend can actually run on this box.

    ``find_spec`` rather than a real import: callers ask this while building
    the daemon, and importing librosa costs seconds. It answers the question
    ``_require_deps`` raises on, one step earlier and without the cost, so a
    build that ships without the ``analysis`` extra can decline to arm a loop
    whose every attempt would end in BackendNotAvailable.
    """
    return all(
        importlib.util.find_spec(name) is not None
        for name in DEFAULT_BACKEND_MODULES
    )


def register(name: str, cls: "type[AnalyzerBackend]") -> None:
    """Idempotent registration."""
    BACKENDS[name] = cls


def get_backend(name: str) -> "type[AnalyzerBackend]":
    """Lazy lookup.  Imports the named backend module if not yet loaded."""
    if name not in BACKENDS:
        # Lazy-import each independently so a preload of one backend does
        # not block the other.
        if name == "librosa":
            from . import librosa
        elif name == "librosa+madmom":
            from . import librosa_madmom
        elif name == "mik":
            from . import mik
        else:
            from . import (
                librosa,  # noqa: F401
                librosa_madmom,  # noqa: F401
                mik,  # noqa: F401
            )
    try:
        return BACKENDS[name]
    except KeyError as exc:
        avail = ", ".join(sorted(BACKENDS)) or "<none>"
        raise KeyError(
            f"Unknown analyser backend {name!r}; available: {avail}"
        ) from exc


__all__ = [
    "BACKENDS",
    "DEFAULT_BACKEND",
    "DEFAULT_BACKEND_MODULES",
    "default_backend_installed",
    "get_backend",
    "register",
]
