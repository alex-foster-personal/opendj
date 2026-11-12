"""Crash blacklist for external stem engines, kept in a JSON file.

When an external engine's subprocess exits non-zero, its engine id is written to the
blacklist file with an expiry. Engine selection refuses an engine while its entry is
current, so a crashing engine stays visibly refused instead of being retried into a
silent success. An entry stops blocking once it expires or is cleared.

File shape: ``{"schema_version": 1, "entries": {engine_id: {"failed_at_s", "expires_at_s",
"exit_code", "argv0"}}}``. Times are epoch seconds supplied by the caller.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

BLACKLIST_SCHEMA_VERSION: int = 1
DEFAULT_BLACKLIST_TTL_S: float = 24 * 60 * 60


class CrashBlacklistError(ValueError):
    """The blacklist file exists but does not have the documented shape."""


class EngineCrashed(RuntimeError):
    """An engine subprocess exited non-zero and was blacklisted."""

    def __init__(self, engine_id: str, exit_code: int) -> None:
        super().__init__(f"stem engine {engine_id!r} exited with code {exit_code}; blacklisted")
        self.engine_id = engine_id
        self.exit_code = exit_code


@dataclass(frozen=True)
class CrashEntry:
    """One engine's most recent crash and when its refusal ends."""

    failed_at_s: float
    expires_at_s: float
    exit_code: int
    argv0: str

    def is_current(self, now_s: float) -> bool:
        return now_s < self.expires_at_s


# -----------------------------------------------------------------------------
# File


def load_blacklist(path: Path) -> dict[str, CrashEntry]:
    """Return every entry in the blacklist file (current or expired); empty when absent."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != BLACKLIST_SCHEMA_VERSION:
        raise CrashBlacklistError(f"{path}: unsupported crash blacklist schema")
    entries = data.get("entries")
    if not isinstance(entries, dict):
        raise CrashBlacklistError(f"{path}: entries must be an object")
    try:
        return {engine_id: CrashEntry(**fields) for engine_id, fields in entries.items()}
    except TypeError as error:
        raise CrashBlacklistError(f"{path}: malformed entry") from error


def _write_blacklist(path: Path, entries: dict[str, CrashEntry]) -> None:
    body = {
        "schema_version": BLACKLIST_SCHEMA_VERSION,
        "entries": {engine_id: asdict(entry) for engine_id, entry in sorted(entries.items())},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def record_crash(
    path: Path, engine_id: str, exit_code: int, argv0: str, now_s: float, ttl_s: float = DEFAULT_BLACKLIST_TTL_S
) -> CrashEntry:
    """Write a current entry for ``engine_id`` to the blacklist file and return it."""
    if ttl_s <= 0:
        raise ValueError(f"ttl_s must be positive, got {ttl_s}")
    entries = load_blacklist(path)
    entry = CrashEntry(failed_at_s=now_s, expires_at_s=now_s + ttl_s, exit_code=exit_code, argv0=argv0)
    entries[engine_id] = entry
    _write_blacklist(path, entries)
    return entry


def clear_entry(path: Path, engine_id: str) -> bool:
    """Remove ``engine_id`` from the blacklist file; return whether it was there."""
    entries = load_blacklist(path)
    if engine_id not in entries:
        return False
    del entries[engine_id]
    _write_blacklist(path, entries)
    return True


# -----------------------------------------------------------------------------
# Running an engine


def run_engine(
    path: Path, engine_id: str, argv: Sequence[str], now_s: float, ttl_s: float = DEFAULT_BLACKLIST_TTL_S
) -> subprocess.CompletedProcess[bytes]:
    """Run an engine subprocess; on a non-zero exit, blacklist it and raise ``EngineCrashed``."""
    completed = subprocess.run(list(argv), check=False, capture_output=True)
    if completed.returncode != 0:
        record_crash(path, engine_id, completed.returncode, argv[0], now_s, ttl_s)
        raise EngineCrashed(engine_id, completed.returncode)
    return completed
