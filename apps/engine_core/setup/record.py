"""Facade: setup.json helpers live in ``apps.shared.setup.record``."""

from __future__ import annotations

from apps.shared.setup.record import (
    RECORD_NAME,
    RECORD_VERSION,
    SetupRecord,
    SetupRecordError,
    read,
    record_path,
    set_dismissed,
    set_folder_watch_roots,
    set_last_import,
    write,
)

__all__ = [
    "RECORD_NAME",
    "RECORD_VERSION",
    "SetupRecord",
    "SetupRecordError",
    "read",
    "record_path",
    "set_dismissed",
    "set_folder_watch_roots",
    "set_last_import",
    "write",
]
