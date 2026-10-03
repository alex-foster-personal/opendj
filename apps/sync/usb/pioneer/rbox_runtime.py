"""Fetch the GPLv3 ``rbox`` binding on first USB-export use.

``rbox`` is GPL-3.0-only. This package is Apache-2.0, so the desktop
payload does not ship it (issue #5143). When ``import rbox`` already
works (a dev extra install, or a previous download), that module is
returned. Otherwise this downloads the pinned wheel into the library
data directory and imports it from there.

The library data directory is :data:`apps.shared.paths.DATA_DIR` (the
``MDT_DATA_DIR`` override, else ``<repo>/data``). That is the same root
:func:`apps.webui.server.deps.get_library_data_dir` derives from the
running app's state database.
"""
from __future__ import annotations

import importlib
import logging
import subprocess
import sys
from pathlib import Path
from types import ModuleType

from apps.shared.paths import DATA_DIR

log = logging.getLogger(__name__)

RBOX_VERSION = "0.1.7"
RBOX_REQUIREMENT = f"rbox=={RBOX_VERSION}"


def vendor_dir() -> Path:
    """Directory a first-use download installs ``rbox`` into."""
    return DATA_DIR / "vendor-gpl" / f"rbox-{RBOX_VERSION}"


def _manual_command() -> str:
    return f"{sys.executable} -m pip install --no-deps {RBOX_REQUIREMENT}"


def _failure(detail: object) -> RuntimeError:
    return RuntimeError(
        f"{RBOX_REQUIREMENT} is GPL-3.0-only and is not distributed in the "
        "Apache-2.0 desktop app. Automatic install failed "
        f"({detail}). Install it manually: {_manual_command()}"
    )


def ensure_rbox() -> ModuleType:
    """Return the ``rbox`` module, downloading the pinned wheel if needed.

    Raises:
        RuntimeError: pip is unavailable or the install does not leave an
            importable ``rbox``. The message names the GPL reason and the
            manual install command. There is no fallback reader.
    """
    try:
        return importlib.import_module("rbox")
    except ImportError:
        pass

    target = vendor_dir()
    target.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--no-deps",
        "--target",
        str(target),
        RBOX_REQUIREMENT,
    ]
    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
    except OSError as exc:
        raise _failure(exc) from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
        raise _failure(detail)

    log.info("Downloaded GPLv3 component %s", RBOX_REQUIREMENT)
    target_text = str(target)
    if target_text not in sys.path:
        sys.path.insert(0, target_text)
    importlib.invalidate_caches()
    sys.modules.pop("rbox", None)
    try:
        return importlib.import_module("rbox")
    except ImportError as exc:
        raise _failure(exc) from exc


__all__ = ["RBOX_REQUIREMENT", "RBOX_VERSION", "ensure_rbox", "vendor_dir"]
