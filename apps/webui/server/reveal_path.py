"""Reveal a track's local file in the OS file manager."""

from __future__ import annotations

import platform
import subprocess
from pathlib import Path

from apps.shared.platform_paths import is_unplayable_path


class RevealPathError(Exception):
    """Named failure for POST /api/v1/tracks/{stable_id}:reveal."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def reveal_track_path(file_path: str | None) -> None:
    """Open the OS file manager on ``file_path`` or raise ``RevealPathError``."""
    if not file_path or is_unplayable_path(file_path):
        raise RevealPathError("not_a_local_file", "track has no local file path")
    path = Path(file_path)
    if not path.exists():
        raise RevealPathError("file_missing", f"file not found: {file_path}")
    system = platform.system()
    if system == "Darwin":
        argv = ["open", "-R", str(path)]
    elif system == "Windows":
        argv = ["explorer", f"/select,{path}"]
    else:
        argv = ["xdg-open", str(path.parent)]
    try:
        subprocess.run(argv, check=True, timeout=10)
    except FileNotFoundError as exc:
        raise RevealPathError(
            "reveal_unavailable", f"reveal command unavailable: {argv}"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise RevealPathError("reveal_unavailable", f"reveal failed: {exc}") from exc
