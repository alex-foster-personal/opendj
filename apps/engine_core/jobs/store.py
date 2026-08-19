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
import logging
import sqlite3
import threading
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from apps.engine_core.jobs.reap import ReapResult, WorkerIdentity, reap_group
from apps.shared import events

log = logging.getLogger(__name__)

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

# finish() is the ONLY terminal write, so the statuses it is allowed to write
# OVER are enumerated rather than left to the caller. 'queued' is excluded
# because queued -> running is claim_queued's transition and skipping it would
# terminate a row no worker ever touched; the terminal statuses are excluded
# because a second finish silently erases what actually happened the first
# time. 'unknown' stays legal: that is the reconcile path resolving a row the
# engine genuinely could not call.
FINISH_SOURCES: frozenset[str] = frozenset({"running", "cancelling", "unknown"})

RESTART_ERROR: str = "engine restarted; outcome unknown"

# A row that reached 'unknown' but whose worker group was never verified dead
# is picked up again by the next boot's recovery. Its error says so, rather
# than re-claiming this was a fresh restart.
REAP_RETRY_ERROR: str = (
    "engine restarted again; the worker group was still unreaped"
)

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

        That commit-then-kill split is exactly why this method is RE-ENTERABLE.
        Between the commit and the last kill the engine can die, or one row's
        reap can raise (a macOS zombie group answers killpg with
        PermissionError; psutil raises ValueError on a non-positive pid), and
        the rows behind it must not lose their only chance at cleanup. So:

          * each row's reap is isolated -- a failure lands in THAT row's error
            column and the loop moves on;
          * ``worker_pgid`` is cleared only once a reap has verified nothing of
            ours is still in the group, which makes the column the record of
            what is still owed;
          * the recovery SELECT therefore also picks up 'unknown' rows that
            still carry a pgid, so an unfinished reap is retried on the next
            boot instead of being abandoned.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                foreign = [
                    dict(row)
                    for row in self._conn.execute(
                        "SELECT * FROM jobs WHERE status IN (?, ?) "
                        "AND owner_boot_id != ?",
                        (*LIVE_STATUSES, self.boot_id),
                    )
                ]
                if foreign:
                    self._conn.executemany(
                        "UPDATE jobs SET status='unknown', error=?, "
                        "finished_at=? WHERE id=?",
                        [(RESTART_ERROR, _now(), row["id"]) for row in foreign],
                    )
                # Read AFTER the flip, so it covers this boot's fresh orphans
                # and any row a previous boot never finished reaping.
                owed = {
                    str(row["id"]): dict(row)
                    for row in self._conn.execute(
                        "SELECT * FROM jobs WHERE status='unknown' "
                        "AND worker_pgid IS NOT NULL"
                    )
                }
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

        fresh_ids = {str(row["id"]) for row in foreign}
        targets = [owed.get(str(row["id"]), row) for row in foreign]
        targets += [row for job_id, row in owed.items() if job_id not in fresh_ids]

        recovered: list[dict[str, Any]] = []
        for row in targets:
            job_id = str(row["id"])
            prefix = RESTART_ERROR if job_id in fresh_ids else REAP_RETRY_ERROR
            result = self._reap_row_safely(row)
            recovered.append(
                self._record_reap(
                    job_id,
                    f"{prefix}. {result.outcome}",
                    clear_pgid=result.group_cleared,
                )
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
        """Terminal write, gated on the SOURCE status being a legal one.

        The check and the write are one BEGIN IMMEDIATE. Reading the status
        in a separate statement would let a concurrent transition land in the
        gap, which is how a late finish used to overwrite a row that had
        already moved on.
        """
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"{status!r} is not a terminal status")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._assert_finishable(job_id, status)
                self._conn.execute(
                    "UPDATE jobs SET status=?, error=?, finished_at=? "
                    "WHERE id=?",
                    (status, error, _now(), job_id),
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return self._emit(job_id)

    def begin_cancel(self, job_id: str) -> dict[str, Any]:
        """running -> cancelling. The row is only 'cancelled' once the group
        is confirmed dead, so a stuck worker never reads as tidily stopped."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._assert_cancellable(job_id)
                self._conn.execute(
                    "UPDATE jobs SET status='cancelling' WHERE id=?", (job_id,)
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return self._emit(job_id)

    def reenqueue(
        self, job_id: str, *, expect_attempt: int | None = None
    ) -> dict[str, Any]:
        """Reset a terminal row back to 'queued' with attempt+1.

        The terminal-status check and the transition are ONE BEGIN IMMEDIATE,
        so two concurrent re-enqueues cannot both pass the check and stack two
        attempts (and, once the supervisor claims them, two workers) onto one
        row. ``expect_attempt`` makes the swap conditional on the exact row
        the caller inspected.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._assert_requeueable(job_id, expect_attempt)
                self._requeue(job_id)
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return self._emit(job_id)

    def resolve_and_requeue(
        self,
        job_id: str,
        *,
        expect_status: str,
        expect_attempt: int,
        resolution: str,
        resolution_error: str,
    ) -> dict[str, Any]:
        """<expect_status> -> <resolution> -> queued as ONE compare-and-swap.

        Two callers can both read the same 'unknown' row and both ask a
        reconcile hook what really happened. Only one of them may act on that
        reading, so the transition is conditional on the status AND attempt
        the caller actually saw. The loser gets JobConflict instead of
        stamping its stale verdict over a row the winner already put back in
        flight -- which used to force a RUNNING row through failed -> queued,
        spawning a second worker while NULLing the first one's pgid so nothing
        could ever reap it.

        One event is published, for the final queued row: the intermediate
        resolution never exists durably, so announcing it would advertise a
        state no reader could ever have observed.
        """
        if resolution not in TERMINAL_STATUSES:
            raise ValueError(f"{resolution!r} is not a terminal status")
        if expect_status not in FINISH_SOURCES:
            raise ValueError(
                f"{expect_status!r} is not a status finish() may resolve"
            )
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._assert_unchanged(job_id, expect_status, expect_attempt)
                self._conn.execute(
                    "UPDATE jobs SET status=?, error=?, finished_at=? "
                    "WHERE id=?",
                    (resolution, resolution_error, _now(), job_id),
                )
                self._requeue(job_id)
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return self._emit(job_id)

    # ----- internals -----------------------------------------------------
    def _row_state(self, job_id: str) -> sqlite3.Row:
        """status + attempt, read INSIDE the caller's open transaction."""
        row = self._conn.execute(
            "SELECT status, attempt FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise JobNotFound(f"no job {job_id!r}")
        return row

    def _assert_finishable(self, job_id: str, status: str) -> None:
        current = str(self._row_state(job_id)["status"])
        if current not in FINISH_SOURCES:
            raise JobConflict(
                f"job {job_id} is {current!r}; finish() only writes over "
                f"{sorted(FINISH_SOURCES)}, so this {status!r} would erase an "
                "outcome that is already recorded"
            )

    def _assert_cancellable(self, job_id: str) -> None:
        current = str(self._row_state(job_id)["status"])
        if current != "running":
            raise JobConflict(
                f"job {job_id} is {current}; only a running job cancels"
            )

    def _assert_unchanged(
        self, job_id: str, expect_status: str, expect_attempt: int
    ) -> None:
        row = self._row_state(job_id)
        if (
            str(row["status"]) != expect_status
            or int(row["attempt"]) != expect_attempt
        ):
            raise JobConflict(
                f"job {job_id} is now {row['status']!r} attempt "
                f"{row['attempt']}, not the {expect_status!r} attempt "
                f"{expect_attempt} that was reconciled; another caller "
                "resolved it first, so this one is refused rather than "
                "applied on top"
            )

    def _assert_requeueable(
        self, job_id: str, expect_attempt: int | None
    ) -> None:
        row = self._row_state(job_id)
        status = str(row["status"])
        if status not in TERMINAL_STATUSES:
            raise JobConflict(
                f"job {job_id} is {status}; only a terminal job re-enqueues"
            )
        if expect_attempt is not None and int(row["attempt"]) != expect_attempt:
            raise JobConflict(
                f"job {job_id} is on attempt {row['attempt']}, not the "
                f"{expect_attempt} this caller inspected; it was re-enqueued "
                "by someone else first"
            )

    def _requeue(self, job_id: str) -> None:
        """The queued-reset UPDATE. Callers own the surrounding transaction."""
        self._conn.execute(
            "UPDATE jobs SET status='queued', progress=0, message=NULL, "
            "error=NULL, attempt=attempt+1, started_at=NULL, "
            "finished_at=NULL, owner_pid=?, owner_boot_id=?, "
            "worker_pid=NULL, worker_pgid=NULL, worker_argv=NULL, "
            "worker_started_at=NULL WHERE id=?",
            (self.owner_pid, self.boot_id, job_id),
        )

    def _reap_row_safely(self, row: dict[str, Any]) -> ReapResult:
        """One row's reap, isolated so it cannot strand the rows behind it.

        Boot recovery is the ONLY chance these rows get. An exception escaping
        here used to abort the loop, leaving every later row's worker alive
        with its status already committed as 'unknown' -- unreaped, and never
        looked at again. The catch is deliberately broad because the failure
        modes are open-ended (killpg answering PermissionError for a macOS
        zombie group, psutil raising ValueError for a non-positive pid, and
        whatever the next OS quirk turns out to be). Nothing is swallowed: the
        error is logged AND written to the row, and group_cleared=False keeps
        the row queued for another attempt on the next boot.
        """
        try:
            return self._reap_row(row)
        except Exception as exc:
            log.exception("reap failed for job %s", row.get("id"))
            return ReapResult(
                f"reap raised {type(exc).__name__}: {exc}. The worker group is "
                "still unaccounted for and will be retried on the next boot",
                group_cleared=False,
            )

    def _reap_row(self, row: dict[str, Any]) -> ReapResult:
        if row.get("worker_pgid") is None:
            return ReapResult(
                "no worker pgid recorded; nothing to reap", group_cleared=True
            )
        argv_raw = row.get("worker_argv")
        started_at = row.get("worker_started_at")
        if not argv_raw or started_at is None:
            return ReapResult(
                "worker identity incomplete (argv or start time missing); "
                "refused to kill an unverifiable pgid",
                group_cleared=False,
            )
        identity = WorkerIdentity(
            pgid=int(row["worker_pgid"]),
            argv=tuple(json.loads(argv_raw)),
            started_at=float(started_at),
        )
        return reap_group(identity)

    def _record_reap(
        self, job_id: str, error: str, *, clear_pgid: bool
    ) -> dict[str, Any]:
        """Write the reap outcome, and retire the pgid only if it is settled.

        worker_pgid doubles as the "this group still owes a reap" flag, so it
        survives anything short of a verified clear. The pgid itself is not
        lost -- the outcome sentence in ``error`` names it.
        """
        with self._lock:
            if clear_pgid:
                self._conn.execute(
                    "UPDATE jobs SET error=?, worker_pgid=NULL WHERE id=?",
                    (error, job_id),
                )
            else:
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
    "FINISH_SOURCES",
    "JOBS_TOPIC",
    "LIVE_STATUSES",
    "REAP_RETRY_ERROR",
    "RESTART_ERROR",
    "STATUSES",
    "TERMINAL_STATUSES",
    "JobConflict",
    "JobNotFound",
    "JobStore",
    "open_store",
    "statuses",
]
