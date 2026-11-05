"""Where the packaged app's engine keeps its data, per OS.

One rule for the Python side (the ``opendj`` CLI and MCP find the running
engine through ``<data dir>/.engine.lock``). The Electron shell's twin is
``apps/desktop/electron/src/data-dir.ts``; ``tests/shared/test_engine_data_dir.py``
asserts the two resolve the same path. ADR windows-data-dir-and-app-identity.

    macOS    ~/Library/Application Support/<identifier>   (Tauri app_data_dir)
    Linux    $XDG_DATA_HOME/<identifier>, else ~/.local/share/<identifier>
    Windows  %LOCALAPPDATA%\\<identifier>                  (never Roaming)

Fail-fast contract: an unset or relative ``LOCALAPPDATA`` on Windows and an
unknown platform raise :class:`EngineDataDirError`; nothing is guessed.

This module has no ``apps.*`` imports, so ``apps.shared.engine_origin`` can use
it without closing a package cycle (see that module's docstring).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path, PurePosixPath, PureWindowsPath

# The bundle identifier the Tauri app uses; the library identity, deliberately
# NOT the Electron appId (those differ on Windows, ADR windows-data-dir-and-app-identity).
ENGINE_IDENTIFIER = "com.opendj.desktop"


class EngineDataDirError(RuntimeError):
    """The engine data dir cannot be resolved on this platform and environment."""


def engine_data_dir(
    *,
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """The packaged engine's data dir for ``platform`` (default: this process)."""
    resolved_platform = sys.platform if platform is None else platform
    environ = os.environ if env is None else env

    if resolved_platform == "win32":
        local_app_data = environ.get("LOCALAPPDATA", "").strip()
        if local_app_data == "":
            raise EngineDataDirError(
                "LOCALAPPDATA is unset; the Windows engine data dir lives under %LOCALAPPDATA% and has no fallback."
            )
        if not PureWindowsPath(local_app_data).is_absolute():
            raise EngineDataDirError(f"LOCALAPPDATA must be an absolute path, got {local_app_data!r}.")
        return Path(PureWindowsPath(local_app_data) / ENGINE_IDENTIFIER)

    home_dir = Path.home() if home is None else home
    if resolved_platform == "darwin":
        return Path(PurePosixPath(home_dir.as_posix()) / "Library" / "Application Support" / ENGINE_IDENTIFIER)
    if resolved_platform.startswith("linux"):
        xdg = environ.get("XDG_DATA_HOME", "").strip()
        base = PurePosixPath(xdg) if xdg != "" else PurePosixPath(home_dir.as_posix()) / ".local" / "share"
        return Path(base / ENGINE_IDENTIFIER)
    raise EngineDataDirError(f"no engine data dir rule for platform {resolved_platform!r}.")
