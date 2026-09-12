"""A focused, durable JSONL stream for engine warnings and errors."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

MAX_BYTES = 5 * 1024 * 1024
RETENTION_DAYS = 7
MAX_TOTAL_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024


def _archive_timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _archive_paths(path: Path) -> list[Path]:
    base = path.name
    parent = path.parent
    archives: list[Path] = []
    for candidate in parent.glob(f"{base}.*"):
        if candidate == path:
            continue
        archives.append(candidate)
    return archives


def _prune_archives(path: Path) -> None:
    cutoff = datetime.now(UTC) - timedelta(days=RETENTION_DAYS)
    archives = _archive_paths(path)
    for archive in archives:
        mtime = datetime.fromtimestamp(archive.stat().st_mtime, tz=UTC)
        if mtime < cutoff:
            archive.unlink(missing_ok=True)
    archives = _archive_paths(path)
    total = sum(item.stat().st_size for item in archives if item.exists())
    while total > MAX_TOTAL_ARCHIVE_BYTES and archives:
        oldest = min(archives, key=lambda item: item.stat().st_mtime)
        size = oldest.stat().st_size
        oldest.unlink(missing_ok=True)
        total -= size
        archives = _archive_paths(path)


def _rotate_if_needed(path: Path, next_bytes: int) -> None:
    size = path.stat().st_size if path.exists() else 0
    if size == 0 or size + next_bytes <= MAX_BYTES:
        return
    archive = path.with_name(f"{path.name}.{_archive_timestamp()}")
    path.rename(archive)
    _prune_archives(path)


class _WarningJsonHandler(logging.Handler):
    def __init__(self, path: Path, boot_id: str) -> None:
        super().__init__(level=logging.WARNING)
        self.path = path
        self.boot_id = boot_id

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self, "_opendj_sink_busy", False):
            return
        message = self.format(record)
        identity: dict[str, str] = {}
        try:
            from apps.shared.telemetry.sink import (
                append_sink,
                make_event,
                maybe_send_sentry,
            )

            event = make_event(
                message=message,
                source_site=f"engine:{record.name}",
                kind="engine",
            )
            identity = {
                "error_id": event.error_id,
                "host": event.host,
                "build_sha": event.build_sha,
            }
            if record.levelno >= logging.ERROR:
                self._opendj_sink_busy = True
                try:
                    append_sink(event)
                    maybe_send_sentry(event)
                finally:
                    self._opendj_sink_busy = False
        except (OSError, ValueError, TypeError, ImportError):
            identity = {}
        entry = {
            "boot_id": self.boot_id,
            "level": record.levelname,
            "logger": record.name,
            "message": message,
            "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
            **identity,
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
