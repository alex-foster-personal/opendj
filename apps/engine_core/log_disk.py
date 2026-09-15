"""Low-disk rotation gate shared by engine log modules."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

ENGINE_LOG_MIN_FREE_BYTES = 1 * 1024 * 1024 * 1024

_rotation_disabled = False
_low_disk_logged = False


def disk_free_bytes(path: Path) -> int:
    target = path if path.is_dir() else path.parent
    return shutil.disk_usage(target).free


def rotation_allowed(path: Path) -> bool:
    global _rotation_disabled
    if _rotation_disabled:
        return False
    mount = path if path.is_dir() else path.parent
    free = disk_free_bytes(mount)
    if free < ENGINE_LOG_MIN_FREE_BYTES:
        _rotation_disabled = True
        _log_low_disk_once(mount, free)
        return False
    return True


def _log_low_disk_once(mount: Path, free_bytes: int) -> None:
    global _low_disk_logged
    if _low_disk_logged:
        return
    _low_disk_logged = True
    logging.getLogger("apps.engine_core.log_disk").error(
        "engine log rotation disabled: %s has %d bytes free (minimum %d)",
        mount,
        free_bytes,
        ENGINE_LOG_MIN_FREE_BYTES,
    )


def reset_rotation_state_for_tests() -> None:
    global _rotation_disabled, _low_disk_logged
    _rotation_disabled = False
    _low_disk_logged = False
