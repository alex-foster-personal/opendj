"""A focused, durable JSONL stream for engine warnings and errors."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

MAX_BYTES = 5 * 1024 * 1024
ROTATIONS = 5


def _rotate_if_needed(path: Path, next_bytes: int) -> None:
    size = path.stat().st_size if path.exists() else 0
    if size == 0 or size + next_bytes <= MAX_BYTES:
        return
    oldest = Path(f"{path}.{ROTATIONS}")
    if oldest.exists():
        oldest.unlink()
    for index in range(ROTATIONS - 1, 0, -1):
        source = Path(f"{path}.{index}")
        if source.exists():
            source.rename(Path(f"{path}.{index + 1}"))
    path.rename(Path(f"{path}.1"))


class _WarningJsonHandler(logging.Handler):
    def __init__(self, path: Path, boot_id: str) -> None:
        super().__init__(level=logging.WARNING)
        self.path = path
        self.boot_id = boot_id

    def emit(self, record: logging.LogRecord) -> None:
        entry = {
            "boot_id": self.boot_id,
            "level": record.levelname,
            "logger": record.name,
            "message": self.format(record),
            "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
        _rotate_if_needed(self.path, len(encoded.encode("utf-8")))
        with self.path.open("a", encoding="utf-8") as output:
            output.write(encoded)


def configure_warning_log(path: Path, boot_id: str) -> logging.Handler:
    """Route root and uvicorn errors into one warn-and-above JSONL file."""
    handler = _WarningJsonHandler(path, boot_id)
    root = logging.getLogger()
    root.addHandler(handler)
    uvicorn_error = logging.getLogger("uvicorn.error")
    uvicorn_error.addHandler(handler)
    uvicorn_error.propagate = False
    return handler
