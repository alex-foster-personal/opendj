"""Where the backfill puts its batches and how much free disk it must leave.

Two values a person may need to change per library, so they are read from a
real file and not only from code: ``<data dir>/state/mik-backfill.json``::

    {"ingest_root": "/Volumes/Scratch/_ingest", "min_free_bytes": 21474836480}

Both keys are optional. Order of authority, strongest first: an explicit
argument on the call (CLI ``--ingest-root`` / ``--min-free-bytes``, the route
body, the MCP tool), then this file, then the built-in default. An unknown key
or a wrong type is an error, never a silent default.

The disk floor is a pure function of three explicit numbers
(:func:`check_disk_floor`), so it can be tested on both sides of the line
without depending on how full the machine running the test happens to be.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .backfill_picture import BackfillError

SETTINGS_FILE = "mik-backfill.json"
DEFAULT_INGEST_ROOT: Path = Path.home() / "Music" / "_ingest"
DEFAULT_MIN_FREE_BYTES: int = 10 * 1024**3
_KEYS = ("ingest_root", "min_free_bytes")


@dataclass(frozen=True)
class Settings:
    ingest_root: Path = DEFAULT_INGEST_ROOT
    """Where batch folders are made."""
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES
    """``stage`` refuses when the copies would leave less free disk than this."""


def settings_path(data_dir: Path) -> Path:
    return data_dir / "state" / SETTINGS_FILE


def _checked_floor(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BackfillError(f"{where}: min_free_bytes must be a whole number of bytes, 0 or more, got {value!r}")
    return value


def _checked_root(value: Any, where: str) -> Path:
    if not isinstance(value, str) or not Path(value).expanduser().is_absolute():
        raise BackfillError(f"{where}: ingest_root must be an absolute path, got {value!r}")
    return Path(value).expanduser()


def load(data_dir: Path) -> Settings:
    """The library's stored settings; the defaults when the file is absent."""
    path = settings_path(data_dir)
    if not path.is_file():
        return Settings()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BackfillError(f"{path}: not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise BackfillError(f"{path}: must be a JSON object")
    unknown = sorted(set(raw) - set(_KEYS))
    if unknown:
        raise BackfillError(f"{path}: unknown keys {unknown}; known keys are {list(_KEYS)}")
    base = Settings()
    return Settings(
        ingest_root=_checked_root(raw["ingest_root"], str(path)) if "ingest_root" in raw else base.ingest_root,
        min_free_bytes=(
            _checked_floor(raw["min_free_bytes"], str(path)) if "min_free_bytes" in raw else base.min_free_bytes
        ),
    )


def resolve(data_dir: Path, *, ingest_root: Path | None = None, min_free_bytes: int | None = None) -> Settings:
    """Stored settings with any explicit argument laid over them."""
    stored = load(data_dir)
    return Settings(
        ingest_root=_checked_root(str(ingest_root), "argument") if ingest_root is not None else stored.ingest_root,
        min_free_bytes=(
            _checked_floor(min_free_bytes, "argument") if min_free_bytes is not None else stored.min_free_bytes
        ),
    )


def check_disk_floor(*, free_bytes: int, needed_bytes: int, min_free_bytes: int, where: Path) -> None:
    """Refuse when copying ``needed_bytes`` would leave less than the floor free."""
    left = free_bytes - needed_bytes
    if left < min_free_bytes:
        raise BackfillError(
            f"staging {needed_bytes} bytes would leave {left} free on {where}, "
            f"below the floor of {min_free_bytes}; free disk or lower --limit"
        )


__all__ = [
    "DEFAULT_INGEST_ROOT",
    "DEFAULT_MIN_FREE_BYTES",
    "SETTINGS_FILE",
    "Settings",
    "check_disk_floor",
    "load",
    "resolve",
    "settings_path",
]
