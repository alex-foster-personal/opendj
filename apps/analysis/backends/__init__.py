"""Backend registry.  Backends call :func:`register` at import-time."""
from __future__ import annotations

import importlib.util
import os
from typing import TYPE_CHECKING

from .base import BackendNonshippable

if TYPE_CHECKING:
    from .base import AnalyzerBackend

BACKENDS: dict[str, type[AnalyzerBackend]] = {}
# Portable default: librosa + scipy install from PyPI wheels (`analysis`
# extra), unlike the git-HEAD-only madmom dev backend.
DEFAULT_BACKEND: str = "librosa"

#: NATIVE-08. Set to "1" to select a backend named in ``NONSHIPPABLE_BACKENDS``.
#: Never set for a shipped build; a CI bench job that needs one of these
#: backends sets it for that job only.
NONSHIPPABLE_ENV: str = "MDT_BENCH_NONSHIPPABLE"

#: Backends the registry refuses to resolve without ``NONSHIPPABLE_ENV=1``,
#: keyed by name with the licensing reason ``get_backend`` raises verbatim.
#: madmom's own code is BSD-3-Clause, but the pretrained beat/downbeat
#: models ``librosa+madmom`` loads are Creative Commons
#: Attribution-NonCommercial-ShareAlike 4.0 (CC BY-NC-SA), which forbids
#: shipping them in a product that is not itself CC BY-NC-SA.
NONSHIPPABLE_BACKENDS: dict[str, str] = {
    "librosa+madmom": (
        "librosa+madmom loads madmom's pretrained beat/downbeat models, "
        "licensed CC BY-NC-SA 4.0 (non-commercial, share-alike); madmom's "
        "own code is BSD-3-Clause but the models are not, so this backend "
        "can never ship. Set MDT_BENCH_NONSHIPPABLE=1 to select it for "
        "bench/reference use only (see "
        "scripts/beatbench/run_madmom_reference_only.py)."
    ),
}

#: The own beatgrid backfill producer. Spelled out here rather than imported
#: from :mod:`apps.analysis.backends.own_beatgrid`, because this branch decides
#: whether to import that module: importing it to learn its name would defeat
#: the laziness the whole function exists for. The two are held equal by
#: ``tests/analysis_beatgrid/test_backfill_write.py``.
OWN_BEATGRID_BACKEND: str = "own_beatgrid.backfill"

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


def register(name: str, cls: type[AnalyzerBackend]) -> None:
    """Idempotent registration."""
    BACKENDS[name] = cls


def get_backend(name: str) -> type[AnalyzerBackend]:
    """Lazy lookup.  Imports the named backend module if not yet loaded.

    NATIVE-08: checked before the import, on the requested name alone, so a
    prior preload of ``name`` (e.g. via the "load everything" branch below)
    can never leave the gate bypassed on a later call.
    """
    reason = NONSHIPPABLE_BACKENDS.get(name)
    if reason is not None and os.environ.get(NONSHIPPABLE_ENV) != "1":
        raise BackendNonshippable(reason)
    if name not in BACKENDS:
        # Lazy-import each independently so a preload of one backend does
        # not block the other.
        if name == "librosa":
            from . import librosa
        elif name == "librosa+madmom":
            from . import librosa_madmom
        elif name == "mik":
            from . import mik
        elif name == OWN_BEATGRID_BACKEND:
            from . import own_beatgrid
        else:
            from . import (
                librosa,  # noqa: F401
                librosa_madmom,  # noqa: F401
                mik,  # noqa: F401
                own_beatgrid,  # noqa: F401
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
    "NONSHIPPABLE_BACKENDS",
    "NONSHIPPABLE_ENV",
    "OWN_BEATGRID_BACKEND",
    "default_backend_installed",
    "get_backend",
    "register",
]
