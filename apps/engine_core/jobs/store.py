"""Jobs persistence: schema, boot recovery, and every status write.

The jobs db is its OWN file (``<data_dir>/state/jobs.db``) so a jobs
migration can never corrupt the library projection in ``state.db``.

Restart semantics live here. A row that was ``running`` under a PREVIOUS
boot_id is not "failed" -- the engine genuinely does not know whether the
worker finished, so it becomes ``unknown`` and stays visibly unknown until a
per-kind reconcile hook resolves it. Guessing 'failed' would be a silent
fallback that hides a completed side effect.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from apps.engine_core.jobs.reap import WorkerIdentity, reap
from apps.shared import events

STATUSES: tuple[str, ...] = (
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "cancelling",
    "unknown",
)
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"succeeded", "failed", "cancelled", "unknown"}
)
LIVE_STATUSES: tuple[str, ...] = ("running", "cancelling")

RESTART_ERROR: str = "engine restarted; outcome unknown"

JOBS_TOPIC: str = "jobs.updated"

_SCHEMA: str = f"""
CREATE TABLE IF NOT EXISTS jobs (
  id                TEXT PRIMARY KEY,
  kind              TEXT NOT NULL,
  payload           TEXT NOT NULL,
  status            TEXT NOT NULL CHECK (status IN
                      ({",".join(f"'{s}'" for s in STATUSES)})),
  progress          REAL NOT NULL DEFAULT 0,
  message           TEXT,
  error             TEXT,
  attempt           INTEGER NOT NULL DEFAULT 0,
  created_at        TEXT NOT NULL,
  started_at        TEXT,
  finished_at       TEXT,
  owner_pid         INTEGER,
  owner_boot_id     TEXT NOT NULL,
  worker_pid        INTEGER,
  worker_pgid       INTEGER,
  worker_argv       TEXT,
  worker_started_at REAL,
  external_ref      TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status_created
  ON jobs (status, created_at);

CREATE TABLE IF NOT EXISTS engine_instance (
  id         INTEGER PRIMARY KEY CHECK (id = 1),
  boot_id    TEXT NOT NULL,
  pid        INTEGER NOT NULL,
  started_at TEXT NOT NULL
);
"""


class JobNotFound(LookupError):
    """No row with that id."""


class JobConflict(RuntimeError):
    """The row is not in a state that permits the requested transition."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class JobStore:
    """Single-writer sqlite store. One connection, one lock, WAL."""

    def __init__(self, db_path: Path, *, boot_id: str, owner_pid: int) -> None:
        self.db_path = Path(db_path)
        self.boot_id = boot_id
        self.owner_pid = owner_pid
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.db_path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ----- boot recovery -------------------------------------------------
    def recover(self) -> list[dict[str, Any]]:
        """Flip foreign live rows to 'unknown', then reap their workers.

        The status flip runs inside BEGIN IMMEDIATE so a concurrent engine
        cannot interleave. The reaping runs AFTER the transaction commits:
        each kill can take up to a 10s grace, and holding sqlite's write
        lock for that long would stall every reader for no benefit.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rows = [
                    dict(row)
                    for row in self._conn.execute(
                        "SELECT * FROM jobs WHERE status IN (?, ?) "
                        "AND owner_boot_id != ?",
                        (*LIVE_STATUSES, self.boot_id),
                    )
                ]
                if rows:
                    self._conn.executemany(
                        "UPDATE jobs SET status='unknown', error=?, "
                        "finished_at=? WHERE id=?",
                        [(RESTART_ERROR, _now(), row["id"]) for row in rows],
                    )
                self._conn.execute("DELETE FROM engine_instance")
                self._conn.execute(
                    "INSERT INTO engine_instance (id, boot_id, pid, started_at)"
                    " VALUES (1, ?, ?, ?)",
                    (self.boot_id, self.owner_pid, _now()),
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

        recovered: list[dict[str, Any]] = []
        for row in rows:
            outcome = self._reap_row(row)
            recovered.append(
                self._append_error(row["id"], f"{RESTART_ERROR}. {outcome}")
            )
        return recovered

    def instance(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM engine_instance WHERE id = 1"
            ).fetchone()
        return dict(row) if row is not None else None

    # ----- reads ---------------------------------------------------------
    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise JobNotFound(f"no job {job_id!r}")
        return _decode(row)

    def list(self, *, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_decode(row) for row in rows]

    def claim_queued(self, *, limit: int = 1) -> list[dict[str, Any]]:
        """Oldest queued rows, marked running under THIS boot_id."""
        claimed: list[dict[str, Any]] = []
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self._conn.execute(
                    "SELECT id FROM jobs WHERE status='queued' "
                    "ORDER BY created_at LIMIT ?",
                    (limit,),
                ).fetchall()
                for row in rows:
                    self._conn.execute(
                        "UPDATE jobs SET status='running', started_at=?, "
                        "owner_pid=?, owner_boot_id=?, error=NULL WHERE id=?",
                        (_now(), self.owner_pid, self.boot_id, row["id"]),
                    )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            claimed = [self._read(row["id"]) for row in rows]
        for job in claimed:
            self._publish(job)
        return claimed

    # ----- writes --------------------------------------------------------
    def enqueue(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        external_ref: str | None = None,
    ) -> dict[str, Any]:
        job_id = str(uuid.uuid4())
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs (id, kind, payload, status, created_at, "
                "owner_pid, owner_boot_id, external_ref) "
                "VALUES (?, ?, ?, 'queued', ?, ?, ?, ?)",
                (
                    job_id,
                    kind,
                    json.dumps(payload, sort_keys=True),
                    _now(),
                    self.owner_pid,
                    self.boot_id,
                    external_ref,
                ),
            )
        return self._emit(job_id)

    def record_worker(
        self, job_id: str, *, pid: int, identity: WorkerIdentity
    ) -> dict[str, Any]:
        """Persist worker identity AT SPAWN so a later reap can prove it."""
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET worker_pid=?, worker_pgid=?, worker_argv=?, "
                "worker_started_at=? WHERE id=?",
                (
                    pid,
                    identity.pgid,
                    json.dumps(list(identity.argv)),
                    identity.started_at,
                    job_id,
                ),
            )
        return self._emit(job_id)

    def set_progress(
        self, job_id: str, progress: float, message: str | None
    ) -> dict[str, Any]:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET progress=?, message=? WHERE id=?",
                (float(progress), message, job_id),
            )
        return self._emit(job_id)

    def finish(
        self, job_id: str, status: str, *, error: str | None = None
    ) -> dict[str, Any]:
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"{status!r} is not a terminal status")
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status=?, error=?, finished_at=? WHERE id=?",
                (status, error, _now(), job_id),
            )
        return self._emit(job_id)

    def begin_cancel(self, job_id: str) -> dict[str, Any]:
        """running -> cancelling. The row is only 'cancelled' once the group
        is confirmed dead, so a stuck worker never reads as tidily stopped."""
        job = self.get(job_id)
        if job["status"] != "running":
            raise JobConflict(
                f"job {job_id} is {job['status']}; only a running job cancels"
            )
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status='cancelling' WHERE id=?", (job_id,)
            )
        return self._emit(job_id)

    def reenqueue(self, job_id: str) -> dict[str, Any]:
        """Reset a terminal row back to 'queued' with attempt+1."""
        job = self.get(job_id)
        if job["status"] not in TERMINAL_STATUSES:
            raise JobConflict(
                f"job {job_id} is {job['status']}; only a terminal job "
                "re-enqueues"
            )
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status='queued', progress=0, message=NULL, "
                "error=NULL, attempt=attempt+1, started_at=NULL, "
                "finished_at=NULL, owner_pid=?, owner_boot_id=?, "
                "worker_pid=NULL, worker_pgid=NULL, worker_argv=NULL, "
                "worker_started_at=NULL WHERE id=?",
                (self.owner_pid, self.boot_id, job_id),
            )
        return self._emit(job_id)

    # ----- internals -----------------------------------------------------
    def _reap_row(self, row: dict[str, Any]) -> str:
        if row.get("worker_pgid") is None:
            return "no worker pgid recorded; nothing to reap"
        argv_raw = row.get("worker_argv")
        started_at = row.get("worker_started_at")
        if not argv_raw or started_at is None:
            return (
                "worker identity incomplete (argv or start time missing); "
                "refused to kill an unverifiable pgid"
            )
        identity = WorkerIdentity(
            pgid=int(row["worker_pgid"]),
            argv=tuple(json.loads(argv_raw)),
            started_at=float(started_at),
        )
        return reap(identity)

    def _append_error(self, job_id: str, error: str) -> dict[str, Any]:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET error=? WHERE id=?", (error, job_id)
            )
        return self._emit(job_id)

    def _read(self, job_id: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise JobNotFound(f"no job {job_id!r}")
        return _decode(row)

    def _emit(self, job_id: str) -> dict[str, Any]:
        job = self.get(job_id)
        self._publish(job)
        return job

    @staticmethod
    def _publish(job: dict[str, Any]) -> None:
        # Through the seam, never a direct hub reference: with no hub
        # registered there are no WS consumers, so no delivery is correct.
        events.publish(JOBS_TOPIC, job)


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    job = dict(row)
    job["payload"] = json.loads(job["payload"])
    argv = job.get("worker_argv")
    job["worker_argv"] = json.loads(argv) if argv else None
    return job


def open_store(
    db_path: Path, *, boot_id: str, owner_pid: int
) -> JobStore:
    return JobStore(db_path, boot_id=boot_id, owner_pid=owner_pid)


def statuses() -> Iterable[str]:
    return STATUSES


__all__ = [
    "JOBS_TOPIC",
    "LIVE_STATUSES",
    "RESTART_ERROR",
    "STATUSES",
    "TERMINAL_STATUSES",
    "JobConflict",
    "JobNotFound",
    "JobStore",
    "open_store",
    "statuses",
]
