"""Durable owed marker for a deferred CloudSync scheduler round (CLOUDSYNC-09)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from apps.sync_hub.atomic_json import write_json_atomic

OWED_FILENAME = "cloudsync-scheduler-owed"


def owed_path(data_dir: Path) -> Path:
    return Path(data_dir) / "state" / OWED_FILENAME


def mark_scheduler_owed(data_dir: Path) -> None:
    """Record that a scheduler round is owed after pressure shed."""
    path = owed_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, {"marked_at": datetime.now(UTC).isoformat()})


def clear_scheduler_owed(data_dir: Path) -> None:
    """Clear the owed marker after a successful round."""
    owed_path(data_dir).unlink(missing_ok=True)


def scheduler_owed(data_dir: Path) -> bool:
    """True when a pressure-deferred round is still owed."""
    return owed_path(data_dir).is_file()


__all__ = [
    "OWED_FILENAME",
    "clear_scheduler_owed",
    "mark_scheduler_owed",
    "owed_path",
    "scheduler_owed",
]
