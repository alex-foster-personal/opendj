"""Small shared I/O helpers for structure sidecar writers."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now_iso_seconds() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def atomic_write_json(path: Path, doc: Any, *, encoding: str = "utf-8", newline: str = "\n") -> None:
    """Write JSON atomically via a sibling ``.json.tmp`` file."""
    tmp = path.with_suffix(".json.tmp")
    text = json.dumps(doc)
    tmp.write_text(text, encoding=encoding, newline=newline)
    os.replace(tmp, path)
