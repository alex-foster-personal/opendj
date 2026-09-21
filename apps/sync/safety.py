"""Phase 4 seven-rail safety harness (D7).

Rails:
  1. Backup: copy the target DB to ``<path>.bak.<ISO8601>`` before any write.
  2. Process check: abort if Rekordbox or djay Pro is running.
  3. Typed confirm: user passed ``--i-understand-the-risks`` (flag_ok=True).
  4. Post-write verify: each track writer may ``verify_readback()`` after
     writing; on mismatch the session pauses and prompts continue/abort/skip.
  5. Reversal script: emit ``data/sync/reversal/<ISO8601>/reverse.sh``
     incrementally as each track succeeds.
  6. Reason field: every write is tagged with a provenance string.
  7. iCloud coherence (djay only): WAL mtime quiesce + ``brctl status``.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from apps.shared.rekordbox_writeback import require_writeback_enabled

TargetName = Literal["rekordbox", "djay"]


class SafetyAbort(RuntimeError):
    """Raised when any of the seven rails refuses to proceed."""


# ----- P04-03: staged rollout (dry-run -> cautious -> bulk) -------------
# apply_analysis write-back dry-run does not enter LiveWriteSession and must
# not call backup_db. Write-back live apply requires the write-back dry-run
# stamp (``require_writeback_plan``); CSV ``--bulk`` still uses the cautious
# stamp (``require_cautious_before_bulk``).

_ROLLOUT_STAMP_DIR = Path("data/sync/rollout")


def _rollout_stamp(writer: str) -> Path:
    return _ROLLOUT_STAMP_DIR / f"{writer}.cautious-ok"


def mark_cautious_success(writer: str) -> Path:
    """Record that a cautious (``--tracks=...``) live run completed OK.

    Bulk live runs check for this stamp via :func:`require_cautious_before_bulk`.
    """
    stamp = _rollout_stamp(writer)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(datetime.now(UTC).isoformat() + "\n", encoding="utf-8")
    return stamp


def writeback_plan_stamp_path(writer: str) -> Path:
    return _ROLLOUT_STAMP_DIR / f"{writer}.writeback-plan.json"


def mark_writeback_plan(writer: str, plan_hash: str) -> Path:
    """Record that a write-back dry-run plan was produced."""
    stamp = writeback_plan_stamp_path(writer)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(
        json.dumps(
            {
                "plan_hash": plan_hash,
                "at": datetime.now(UTC).isoformat(),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return stamp


def require_writeback_plan(writer: str, plan_hash: str) -> None:
    """Abort write-back live runs without a matching dry-run plan stamp."""
    stamp = writeback_plan_stamp_path(writer)
    if not stamp.exists():
        raise SafetyAbort(
            f"write-back live for {writer!r} requires a dry-run plan stamp at "
            f"{stamp}; run without --live first."
        )
    try:
        data = json.loads(stamp.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SafetyAbort(
            f"write-back dry-run stamp at {stamp} is unreadable: {exc}"
        ) from exc
    if data.get("plan_hash") != plan_hash:
        raise SafetyAbort(
            f"write-back live plan hash mismatch for {writer!r}: dry-run plan "
            "is stale; re-run without --live."
        )


def require_cautious_before_bulk(writer: str, override: bool = False) -> None:
    """Abort bulk live runs that skip the cautious stage.

    Raises :class:`SafetyAbort` unless a prior cautious live run of
    ``writer`` deposited the stamp (or the caller explicitly passed
    ``override=True``, e.g. ``--skip-cautious-check`` after documenting why).
    """
    if override:
        return
    stamp = _rollout_stamp(writer)
    if not stamp.exists():
        raise SafetyAbort(
            f"P04-03: refusing --bulk --live for {writer!r}: no cautious "
            f"success stamp at {stamp}. Run a small --tracks=... live pass "
            f"first, or pass --skip-cautious-check with documented reason."
        )


@dataclass(slots=True)
class WriteRecord:
    track_id: str
    reason: str
    status: Literal["pending", "written", "verified", "failed", "skipped"]
    reverse_snippet: str = ""


def _iso_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _is_running(process_name: str) -> bool:
    try:
        res = subprocess.run(
            ["pgrep", "-x", process_name],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return res.returncode == 0 and bool(res.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def target_process_name(target: TargetName) -> str:
    return {"rekordbox": "Rekordbox", "djay": "djay Pro"}[target]


def assert_target_not_running(target: TargetName) -> None:
    """Rail 2."""
    name = target_process_name(target)
    if _is_running(name):
        raise SafetyAbort(f"{name} is running. Quit it before any live write.")


def brctl_status(path: Path) -> str:
    try:
        res = subprocess.run(
            ["brctl", "status", str(path)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return (res.stdout or "") + (res.stderr or "")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def assert_icloud_quiesced(
    db_path: Path,
    *,
    wal_quiesce_seconds: int = 60,
    brctl_abort_tokens: Iterable[str] = ("error", "uploading", "downloading"),
) -> None:
    """Rail 7 (djay-only): WAL mtime + brctl status gate."""
    wal = Path(str(db_path) + "-wal")
    if wal.exists():
        age = time.time() - wal.stat().st_mtime
        if age < wal_quiesce_seconds:
            raise SafetyAbort(
                f"{wal} modified {age:.1f}s ago; need {wal_quiesce_seconds}s "
                "quiesce. djay may be actively syncing; wait and retry."
            )
    output = brctl_status(db_path.parent)
    lowered = output.lower()
    for token in brctl_abort_tokens:
        if token in lowered and "in sync" not in lowered.split(token)[0][-40:]:
            raise SafetyAbort(
                f"brctl status indicates iCloud activity ({token!r}) on "
                f"{db_path.parent}; wait for sync to settle and retry."
            )


def backup_db(db_path: Path) -> Path:
    """Rail 1: copy the DB aside with a timestamped suffix."""
    if not db_path.exists():
        raise SafetyAbort(f"DB to back up is missing: {db_path}")
    bak = db_path.with_suffix(db_path.suffix + f".bak.{_iso_stamp()}")
    shutil.copy2(db_path, bak)
    return bak


def require_typed_confirm(flag_ok: bool) -> None:
    """Rail 3."""
    if not flag_ok:
        raise SafetyAbort(
            "Typed confirm required. Pass --i-understand-the-risks to proceed."
        )


@dataclass(slots=True)
class _TrackWriter:
    track_id: str
    reason: str
    session: "LiveWriteSession"
    _written_ok: bool = False
    _verified_ok: bool = False

    def write(self, _payload: object) -> None:
        """Record that a write occurred. Auto-marks verified when no verifier."""
        self._written_ok = True
        if self.session.verifier is None:
            self._verified_ok = True

    def verify_readback(self, expected: object | None = None) -> bool:
        verifier = self.session.verifier
        if verifier is None:
            self._verified_ok = True
            return True
        ok = bool(verifier(self.track_id, expected))
        self._verified_ok = ok
        return ok

    def append_reverse(self, snippet: str) -> None:
        self.session._reverse_lines.append(snippet)
        with self.session.reverse_script_path.open("a", encoding="utf-8") as fp:
            fp.write(snippet.rstrip() + "\n")


@dataclass(slots=True)
class LiveWriteSession:
    target: TargetName
    reason: str
    flag_ok: bool
    db_path: Path
    wal_quiesce_seconds: int = 60
    reversal_root: Path | None = None
    verifier: Callable[[str, object | None], bool] | None = None
    process_gate_override: Callable[[TargetName], None] | None = None
    icloud_gate_override: Callable[[Path, int], None] | None = None

    _reverse_dir: Path | None = field(init=False, default=None)
    _reverse_lines: list[str] = field(init=False, default_factory=list)
    _backup_path: Path | None = field(init=False, default=None)
    _failed_tracks: list[str] = field(init=False, default_factory=list)
    _skipped_tracks: list[str] = field(init=False, default_factory=list)
    _written_tracks: list[str] = field(init=False, default_factory=list)

    def __enter__(self) -> "LiveWriteSession":
        # Backstop rail 0: one-way import mode. Every seven-rail live write
        # aimed at rekordbox passes through here, so a caller that forgets its
        # own guard still cannot reach the real library.
        if self.target == "rekordbox":
            require_writeback_enabled("module.sync.safety.live_write_session")
        require_typed_confirm(self.flag_ok)
        if self.process_gate_override is not None:
            self.process_gate_override(self.target)
        else:
            assert_target_not_running(self.target)
        if self.target == "djay":
            if self.icloud_gate_override is not None:
                self.icloud_gate_override(self.db_path, self.wal_quiesce_seconds)
            else:
                assert_icloud_quiesced(
                    self.db_path, wal_quiesce_seconds=self.wal_quiesce_seconds
                )
        self._backup_path = backup_db(self.db_path)

        stamp = _iso_stamp()
        root = self.reversal_root or (self.db_path.parent.parent / "sync" / "reversal")
        self._reverse_dir = root / stamp
        self._reverse_dir.mkdir(parents=True, exist_ok=True)
        self.reverse_script_path.write_text(
            "#!/usr/bin/env bash\n"
            f"# Phase 4 reversal script -- target={self.target} "
            f"reason={self.reason!r}\n"
            f"# Backup: {self._backup_path}\n"
            "set -euo pipefail\n",
            encoding="utf-8",
        )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # [I2 fix] Always append the last-resort restore footer to the reverse
        # script, even when an exception escapes the ``with`` block. Without
        # this, a crash mid-session would leave the user with a partial
        # reverse.sh that lacks the one-liner needed to roll back the whole DB.
        try:
            with self.reverse_script_path.open("a", encoding="utf-8") as fp:
                if exc is not None:
                    exc_name = exc_type.__name__ if exc_type is not None else "Unknown"
                    fp.write(
                        f"\n# NOTE: session aborted with exception "
                        f"{exc_name}: {exc!s}\n"
                    )
                fp.write(
                    "\n# Last-resort full restore (uncomment to use):\n"
                    f"# cp -n '{self._backup_path}' '{self.db_path}'\n"
                )
        except Exception:  # pragma: no cover - best-effort footer write
            # Never let footer-write failures mask the original exception.
            pass
        return None

    @property
    def reverse_script_path(self) -> Path:
        assert self._reverse_dir is not None
        return self._reverse_dir / "reverse.sh"

    @property
    def backup_path(self) -> Path | None:
        return self._backup_path

    @contextmanager
    def per_track(self, track_id: str) -> Iterator[_TrackWriter]:
        writer = _TrackWriter(track_id=track_id, reason=self.reason, session=self)
        try:
            yield writer
        except Exception:
            self._failed_tracks.append(track_id)
            raise
        if writer._written_ok and writer._verified_ok:
            self._written_tracks.append(track_id)
        elif writer._written_ok and not writer._verified_ok:
            action = self._prompt_on_verify_failure(track_id)
            if action == "abort":
                raise SafetyAbort(
                    f"verify_readback failed for {track_id}; batch aborted."
                )
            if action == "skip":
                self._skipped_tracks.append(track_id)
            else:
                self._written_tracks.append(track_id)
        else:
            self._skipped_tracks.append(track_id)

    def _prompt_on_verify_failure(self, track_id: str) -> str:
        if not sys.stdin.isatty():
            return "abort"
        print(
            f"\n[safety] verify failed for {track_id}. "
            "[c]ontinue, [a]bort, [s]kip: ",
            end="",
            flush=True,
        )
        try:
            answer = sys.stdin.readline().strip().lower()[:1]
        except Exception:
            return "abort"
        return {"c": "continue", "a": "abort", "s": "skip"}.get(answer, "abort")

    @property
    def written_tracks(self) -> list[str]:
        return list(self._written_tracks)

    @property
    def skipped_tracks(self) -> list[str]:
        return list(self._skipped_tracks)

    @property
    def failed_tracks(self) -> list[str]:
        return list(self._failed_tracks)


__all__ = [
    "SafetyAbort",
    "LiveWriteSession",
    "WriteRecord",
    "assert_target_not_running",
    "assert_icloud_quiesced",
    "backup_db",
    "mark_writeback_plan",
    "require_typed_confirm",
    "require_writeback_plan",
    "target_process_name",
    "writeback_plan_stamp_path",
]
