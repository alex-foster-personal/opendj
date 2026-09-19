"""Serato write-safety rails.

Implements the 6-rail pattern from ``apps/reconcile/apply.py`` adapted to
Serato:

  1. ``is_serato_running()`` -- pgrep rail; abort writes if Serato is live.
  2. Timestamped backup of target files before mutation.
  3. Reversal log (ndjson) under a gitignored directory.
  4. Typed confirmation for the top-level adapter write entry point.
  5. Post-write re-read verification (done in ``adapter.py``).
  6. Dry-run-first: ``write()`` accepts ``dry_run=True`` by default.

Tests monkeypatch ``_pgrep_serato`` so the CI host's running processes
don't influence the suite.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# ----------------------------------------------------- running-process gate


def _pgrep_serato() -> int | None:
    """Return PID of a running Serato instance, or None."""
    try:
        result = subprocess.run(
            ["pgrep", "-x", "Serato DJ Pro"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return int(result.stdout.strip().splitlines()[0])
    except ValueError:
        return None


def is_serato_running() -> bool:
    """True when a Serato process is detected by pgrep."""
    return _pgrep_serato() is not None


class SeratoRunningError(RuntimeError):
    """Raised when a write is attempted while Serato is running."""


# -------------------------------------------------------------- backup


@dataclass(frozen=True)
class BackupRecord:
    """Record of one file backup step."""

    original_path: Path
    backup_path: Path
    sha256_before: str
    timestamp_iso: str


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def backup_file(target: Path, backup_dir: Path) -> BackupRecord:
    """Copy ``target`` into ``backup_dir`` with a timestamped name.

    The backup filename is ``<sha256-of-abs-path>.<iso-timestamp>.bak`` so
    multiple files from different dirs cannot collide. Returns a record the
    reversal log can point to.
    """
    target = Path(target).resolve()
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    path_key = hashlib.sha256(str(target).encode("utf-8")).hexdigest()[:16]
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"{path_key}.{timestamp}.bak"
    shutil.copy2(target, backup_path)
    return BackupRecord(
        original_path=target,
        backup_path=backup_path,
        sha256_before=_sha256(target),
        timestamp_iso=timestamp,
    )


# --------------------------------------------------------------- reversal


def append_reversal(log_path: Path, record: BackupRecord, action: str) -> None:
    """Append an ndjson line to the reversal log."""
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "action": action,
        "original_path": str(record.original_path),
        "backup_path": str(record.backup_path),
        "sha256_before": record.sha256_before,
        "timestamp": record.timestamp_iso,
    }
    with log_path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(entry, sort_keys=True) + "\n")


# ------------------------------------------------------------- gate helper


def guard_live_write(*, live_write: bool, pgrep_fn=None) -> None:
    """Raise if a live write is unsafe. Monkeypatch ``pgrep_fn`` in tests.

    When ``live_write`` is False, this is a no-op (caller intends a dry-run
    or fixture write). When True, we refuse to proceed if Serato is running.
    """
    if not live_write:
        return
    fn = pgrep_fn or _pgrep_serato
    pid = fn()
    if pid is not None:
        raise SeratoRunningError(
            f"Serato DJ Pro is running (pid={pid}); refuse to write live"
        )
