"""Atomic JSON file writes for the small CloudSync state files in a data dir.

One helper so ``cloudsync-config.json`` and ``cloudsync-heartbeat.json`` are
written the same way: a sibling temp file, flushed and fsynced, then
``os.replace`` onto the target. A reader (another process, a crash restart)
sees the old file or the new one, never a torn half.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` to ``path`` atomically. ``path.parent`` must exist.

    The parent is NOT created here: a data dir that does not exist is almost
    always a typo'd ``--data-dir``, and minting one would hide it.
    """
    if not path.parent.is_dir():
        raise FileNotFoundError(f"{path.parent} is not a directory; refusing to create it")
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


__all__ = ["write_json_atomic"]
