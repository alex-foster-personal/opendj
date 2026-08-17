"""Small, bounded JSONL logs for browser-originated diagnostics."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

DEFAULT_LOG_DIR = Path.home() / ".local/share/music-dj-tools/webui"
MAX_DAILY_LOG_BYTES = 10 * 1024 * 1024


def daily_log_path(log_dir: Path, prefix: str, now: time.struct_time) -> Path:
    return log_dir / f"{prefix}-{time.strftime('%Y-%m-%d', now)}.log"


def append_json_record(path: Path, record: dict[str, object]) -> bool:
    """Append one private JSON line, refusing to exceed the daily cap."""
    line = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        current_size = path.stat().st_size
    except FileNotFoundError:
        current_size = 0
    if current_size + len(line) > MAX_DAILY_LOG_BYTES:
        return False
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, line)
    finally:
        os.close(descriptor)
    return True
