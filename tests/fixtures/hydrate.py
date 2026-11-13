"""Portable verify-then-hydrate helpers for locked test fixtures.

Tests mutate only disposable copies under ``tmp_path``; canonical fixture
sources must stay byte-identical. Stored paths may originate on another OS
(Windows drive letters or UNC on POSIX hosts). Classification and hydration
use :mod:`apps.shared.platform_paths` instead of naive :class:`~pathlib.Path`
checks so foreign absolutes never fabricate a local file.

Future subprocess spawns from this module must use :data:`sys.executable`,
never a hardcoded ``.venv/bin/python`` path.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from apps.shared.platform_paths import MappedPath, PathMap, is_any_absolute, resolve_library_path

__all__ = [
    "classify_stored_path",
    "hydrate_file",
    "is_any_absolute",
]


def hydrate_file(source: Path, destination: Path) -> Path:
    """Copy ``source`` to ``destination`` with :func:`shutil.copy2`.

    Raises :class:`FileNotFoundError` when ``source`` is not an existing file.
    Does not adjust permission bits (portable across Windows and POSIX).
    """
    if not source.is_file():
        raise FileNotFoundError(f"fixture source is not a file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return destination


def classify_stored_path(path_str: str) -> MappedPath:
    """Classify a stored fixture path without resolving it on this host.

    Delegates to :func:`~apps.shared.platform_paths.resolve_library_path`
    with an empty :class:`PathMap` so foreign absolutes return
    ``resolved=None`` and an explicit ``reason`` instead of a fabricated path.
    """
    return resolve_library_path(path_str, path_map=PathMap(entries=()))
