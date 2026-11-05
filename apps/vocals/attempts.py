"""The last failed vocals attempt per track, so a failure is not silence.

An HTTP-triggered Demucs run happens after the 202 has been sent. When it
raised, the only trace was a traceback in the engine log, and the status route
went on answering ``not_analyzed``. This module keeps one small record per
track of the attempt that failed, which the status route reports until the
next attempt starts.

Records live in ``state/vocal-attempts/``, beside the vocal-cache rather than
inside it: every ``*.json`` in the cache directory is read as a cache entry.

Requirements (mini-PRD):
  ✔︎ ✅ record, read and clear one failure per stable id
    [if] an attempt raises [then] its type, bounded reason and UTC time are stored
    [if] the next worker attempt starts, on any path [then] the previous
    failure is cleared; a from-stems derivation clears it when it publishes
    [if] no attempt has failed [then] load_failure returns None
    [if] a record is there but unreadable [then] that is reported as the
    failure, never read as "no failure"
    [if] a stable id would escape the directory [then ⛔️] ValueError

-Claude
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from apps.shared.state.stable_id_paths import confined_stable_id_json_path

ATTEMPTS_DIRNAME: str = "vocal-attempts"
ATTEMPT_SCHEMA: int = 1
REASON_MAX_CHARS: int = 4000


@dataclass(frozen=True)
class FailedAttempt:
    """Why the last attempt for one track did not produce vocals."""

    reason: str
    error_type: str
    failed_at: str | None


def attempts_dir(data_dir: Path) -> Path:
    return data_dir / "state" / ATTEMPTS_DIRNAME


def attempt_path(data_dir: Path, stable_id: str) -> Path:
    return confined_stable_id_json_path(
        attempts_dir(data_dir),
        stable_id,
        directory_name=ATTEMPTS_DIRNAME,
    )


def record_failure(data_dir: Path, stable_id: str, error: BaseException) -> FailedAttempt:
    """Store ``error`` as the track's failed attempt, atomically."""
    attempt = FailedAttempt(
        reason=str(error)[:REASON_MAX_CHARS] or type(error).__name__,
        error_type=type(error).__name__,
        failed_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    path = attempt_path(data_dir, stable_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "schema": ATTEMPT_SCHEMA,
                    "stable_id": stable_id,
                    "reason": attempt.reason,
                    "error_type": attempt.error_type,
                    "failed_at": attempt.failed_at,
                },
                fh,
                indent=1,
            )
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return attempt


def clear_failure(data_dir: Path, stable_id: str) -> None:
    attempt_path(data_dir, stable_id).unlink(missing_ok=True)


def load_failure(data_dir: Path, stable_id: str) -> FailedAttempt | None:
    """The track's recorded failure, or None when no record exists."""
    path = attempt_path(data_dir, stable_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        return _unreadable(path, f"{type(exc).__name__}: {exc}")
    try:
        record = json.loads(raw)
    except json.JSONDecodeError as exc:
        return _unreadable(path, str(exc))
    if (
        not isinstance(record, dict)
        or record.get("schema") != ATTEMPT_SCHEMA
        or not all(isinstance(record.get(key), str) for key in ("reason", "error_type", "failed_at"))
    ):
        return _unreadable(path, "not a schema 1 failed-attempt record")
    return FailedAttempt(reason=record["reason"], error_type=record["error_type"], failed_at=record["failed_at"])


def _unreadable(path: Path, why: str) -> FailedAttempt:
    return FailedAttempt(
        reason=f"an attempt failed, but its record {path.name} cannot be read ({why})",
        error_type="UnreadableAttemptRecord",
        failed_at=None,
    )
