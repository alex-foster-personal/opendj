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
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

from apps.engine_core.jobs.reap import ReapResult, WorkerIdentity, reap_group
from apps.engine_core.utc_timestamps import utc_iso_ms
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
# Not terminal: the row's work is still to come or under way.
ACTIVE_STATUSES: tuple[str, ...] = ("queued", "running", "cancelling")

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

# A claimed row with no recorded pgid. "nothing to reap" was true only for
# the harmless half of that: it also covers a worker that WAS forked, with the
# engine dying before the write that would have named it. Nothing can find
# that process again, so the row is the only place the possibility can be
# recorded and it has to be recorded, not smoothed over.
SPAWN_WINDOW_ERROR: str = (
    "no worker pgid was ever recorded for this row, so there is no reap "
    "target. Two conditions produce that and this engine cannot tell them "
    "apart: no worker was ever forked, or one was and the engine died in the "
    "window between the fork and the write, leaving a live process with "
    "nothing that can now identify it"
)

# A queued row has no worker, so 'cancelled' is the honest terminal status
# rather than the 'unknown' a lost worker earns: there is provably nothing
# running, because nothing was ever spawned.
CANCEL_BEFORE_SPAWN: str = "cancelled while queued; no worker was ever spawned"

JOBS_TOPIC: str = "jobs.updated"

ADDED_COLUMNS: tuple[str, ...] = ("stderr_tail", "outcome")


class ActiveJob(NamedTuple):
    """One active row as a duplicate rule sees it."""

    id: str
    status: str
    payload: dict[str, Any]
    worker_argv: tuple[str, ...] | None = None


DuplicateRule = Callable[[list[ActiveJob]], str | None]

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
  external_ref      TEXT,
  stderr_tail       TEXT,
  outcome           TEXT
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


class ActiveIdOverlap(JobConflict):
    """A non-terminal row of this kind already owns one of the blocked ids."""

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        super().__init__(
            f"an active job already owns some of these ids (job {job_id})"
        )


class JobDuplicate(JobConflict):
    """An ACTIVE row already does the work; ``existing`` is that row."""

    def __init__(self, message: str, existing: dict[str, Any]) -> None:
        super().__init__(message)
        self.existing = existing


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
            self._add_missing_columns()

    def _add_missing_columns(self) -> None:
        """Columns added after a jobs.db was first created (STEM-60).

        ``CREATE TABLE IF NOT EXISTS`` leaves an existing table alone, so an
        installed app's jobs.db gains new columns only here. Additive and
        nullable, so an older engine reading the same file is unaffected.
        """
        have = {row["name"] for row in self._conn.execute("PRAGMA table_info(jobs)")}
        for name in ADDED_COLUMNS:
            if name not in have:
                self._conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} TEXT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ----- boot recovery -------------------------------------------------
    def recover(self) -> list[dict[str, Any]]:
        """Flip foreign live rows to 'unknown', then reap their workers.

        The status flip runs inside BEGIN IMMEDIATE so a concurrent engine
        cannot interleave. The reaping runs AFTER the transaction commits, and
        it is not cheap: each row's kill ladder can spend a full
        WORKER_TERMINATE_GRACE_S (10s), and the rows are reaped SERIALLY on
        this synchronous boot path, so N unreaped rows can cost N x 10s before
        the engine finishes starting. Holding sqlite's write lock across that
        would stall every reader for no benefit.

        That commit-then-kill split is also why this method is RE-ENTERABLE.
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
                        [(RESTART_ERROR, utc_iso_ms(), row["id"]) for row in foreign],
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
                    (self.boot_id, self.owner_pid, utc_iso_ms()),
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
        """Oldest queued rows, marked running under THIS boot_id.

        The UPDATE re-asserts ``status='queued'`` even though the SELECT ran
        inside the same BEGIN IMMEDIATE. That makes the claim a
        compare-and-swap rather than a blind write, which is what
        ``cancel_queued`` races against: a cancelled row must never be
        resurrected into 'running' by a claim that read it a moment earlier.

        A row whose swap matches nothing is DROPPED from the batch instead of
        being reported as claimed. Reporting it would hand the supervisor a
        job to spawn a worker for, which is the whole failure this guards.
        """
        claimed: list[dict[str, Any]] = []
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self._conn.execute(
                    "SELECT id FROM jobs WHERE status='queued' "
                    "ORDER BY created_at LIMIT ?",
                    (limit,),
                ).fetchall()
                won: list[str] = []
                for row in rows:
                    swapped = self._conn.execute(
                        "UPDATE jobs SET status='running', started_at=?, "
                        "owner_pid=?, owner_boot_id=?, error=NULL "
                        "WHERE id=? AND status='queued'",
                        (utc_iso_ms(), self.owner_pid, self.boot_id, row["id"]),
                    )
                    if swapped.rowcount == 1:
                        won.append(str(row["id"]))
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            claimed = [self._read(job_id) for job_id in won]
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
        block_active_ids: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if block_active_ids is None:
                job_id = self._insert(kind, payload, external_ref)
            else:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    self._assert_no_active_id_overlap(
                        kind, set(block_active_ids)
                    )
                    job_id = self._insert(kind, payload, external_ref)
                    self._conn.execute("COMMIT")
                except BaseException:
                    self._conn.execute("ROLLBACK")
                    raise
        return self._emit(job_id)

    def enqueue_unless_active(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        conflicts: DuplicateRule,
        external_ref: str | None = None,
        block_active_ids: Sequence[str] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Insert unless an ACTIVE row of ``kind`` already does this work.

        ``conflicts`` gets the active (queued, running, cancelling) rows of
        the kind as ``ActiveJob(id, status, payload, worker_argv)``, newest first, and names the one that
        already does this work, or None. The read and the insert are ONE BEGIN
        IMMEDIATE, the write lock sqlite takes for the whole file, so two
        requests racing (two tabs, two agents, or two connections to one db)
        cannot both see "nothing active" and both insert: a check in the
        client and then a POST is a check-then-act that both can pass.

        Returns ``(row, True)`` for a new row, or ``(existing_row, False)``
        when ``conflicts`` named an active row and nothing was inserted.
        """
        existing: str | None = None
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if block_active_ids:
                    self._assert_no_active_id_overlap(kind, set(block_active_ids))
                existing = self._active_duplicate(kind, conflicts, exclude=None)
                job_id = (
                    self._insert(kind, payload, external_ref)
                    if existing is None
                    else existing
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        if existing is not None:
            return self.get(existing), False
        return self._emit(job_id), True

    def _active_duplicate(
        self,
        kind: str,
        conflicts: DuplicateRule,
        *,
        exclude: str | None,
    ) -> str | None:
        """The active row of ``kind`` (not ``exclude``) that ``conflicts``
        names. Read INSIDE the caller's open BEGIN IMMEDIATE."""
        rows = self._conn.execute(
            "SELECT id, status, payload, worker_argv FROM jobs WHERE kind = ? AND status IN "
            f"({','.join('?' for _ in ACTIVE_STATUSES)}) "
            "ORDER BY created_at DESC, id DESC",
            (kind, *ACTIVE_STATUSES),
        ).fetchall()
        active = [
            ActiveJob(
                str(row["id"]),
                str(row["status"]),
                json.loads(row["payload"]),
                tuple(json.loads(row["worker_argv"])) if row["worker_argv"] else None,
            )
            for row in rows
            if str(row["id"]) != exclude
        ]
        return conflicts(active)

    def _refuse_duplicate_requeue(
        self, job_id: str, conflicts: DuplicateRule | None
    ) -> str | None:
        """The active row a re-enqueue of ``job_id`` would duplicate, if any."""
        if conflicts is None:
            return None
        kind = self._conn.execute(
            "SELECT kind FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if kind is None:
            raise JobNotFound(f"no job {job_id!r}")
        return self._active_duplicate(str(kind["kind"]), conflicts, exclude=job_id)

    def _duplicate_error(self, job_id: str, existing: str) -> JobDuplicate:
        row = self.get(existing)
        return JobDuplicate(
            f"job {job_id} was not re-enqueued: active job {existing} "
            f"({row['status']}) already does this work",
            row,
        )

    def _insert(
        self, kind: str, payload: dict[str, Any], external_ref: str | None
    ) -> str:
        """One queued row; the caller holds ``self._lock``."""
        job_id = str(uuid.uuid4())
        self._conn.execute(
            "INSERT INTO jobs (id, kind, payload, status, created_at, "
            "owner_pid, owner_boot_id, external_ref) "
            "VALUES (?, ?, ?, 'queued', ?, ?, ?, ?)",
            (
                job_id,
                kind,
                json.dumps(payload, sort_keys=True),
                utc_iso_ms(),
                self.owner_pid,
                self.boot_id,
                external_ref,
            ),
        )
        return job_id

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

    def record_stderr_tail(self, job_id: str, lines: Iterable[str]) -> None:
        """Keep what the worker said on stderr, whatever the ending (STEM-60).

        Not a status write and not gated: cancel() can own the terminal status
        and the worker's own account of itself still belongs on the row.
        """
        text = "\n".join(lines)
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET stderr_tail=? WHERE id=?", (text or None, job_id)
            )

    def finish(
        self,
        job_id: str,
        status: str,
        *,
        error: str | None = None,
        outcome: dict[str, Any] | None = None,
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
                    "UPDATE jobs SET status=?, error=?, finished_at=?, "
                    "outcome=COALESCE(?, outcome) WHERE id=?",
                    (
                        status,
                        error,
                        _now(),
                        None if outcome is None else json.dumps(outcome, sort_keys=True),
                        job_id,
                    ),
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return self._emit(job_id)

    def finish_while_running(
        self,
        job_id: str,
        status: str,
        *,
        progress: float | None = None,
        message: str | None = None,
        error: str | None = None,
        outcome: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Measured progress and ending, only while the row is still running.

        Returns None without writing when the row has left ``running``
        (cancel moves it to ``cancelling``, then ``cancelled``). The status
        read and the update are one BEGIN IMMEDIATE, so a measured
        succeeded or failed ending cannot land on that row, and cannot
        take the terminal write cancel is about to make.
        """
        if status not in ("succeeded", "failed"):
            raise ValueError(
                f"{status!r} is not a measured ending; finish_while_running "
                "writes succeeded or failed only"
            )
        outcome_json = (
            None if outcome is None else json.dumps(outcome, sort_keys=True)
        )
        wrote = False
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                current = str(self._row_state(job_id)["status"])
                if current == "running":
                    if progress is None:
                        updated = self._conn.execute(
                            "UPDATE jobs SET status=?, error=?, finished_at=?, "
                            "outcome=COALESCE(?, outcome) "
                            "WHERE id=? AND status='running'",
                            (status, error, _now(), outcome_json, job_id),
                        )
                    else:
                        updated = self._conn.execute(
                            "UPDATE jobs SET status=?, error=?, finished_at=?, "
                            "outcome=COALESCE(?, outcome), progress=?, message=? "
                            "WHERE id=? AND status='running'",
                            (
                                status,
                                error,
                                _now(),
                                outcome_json,
                                float(progress),
                                message,
                                job_id,
                            ),
                        )
                    self._require_measured_update(updated, job_id)
                    wrote = True
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        if not wrote:
            return None
        return self._emit(job_id)

    def cancel_queued(self, job_id: str) -> dict[str, Any] | None:
        """queued -> cancelled as ONE compare-and-swap, or None if not queued.

        A queued row has no worker, so there is no group whose death has to be
        proven and no reason to route it through 'cancelling': the transition
        is legal and complete in one step. It has to be a compare-and-swap
        because the supervisor's ``claim_queued`` is the thing it is racing --
        exactly one of the two may win, and if the claim wins, this call must
        report that rather than stamping 'cancelled' over a row a worker is
        already running.

        None is that report -- "the row is not queued, take the running path"
        -- and is the ONLY non-queued outcome that is not an error. Every
        illegal status is refused loudly by ``begin_cancel`` a moment later,
        so nothing is swallowed here.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if str(self._row_state(job_id)["status"]) != "queued":
                    self._conn.execute("COMMIT")
                    return None
                self._conn.execute(
                    "UPDATE jobs SET status='cancelled', error=?, "
                    "finished_at=? WHERE id=? AND status='queued'",
                    (CANCEL_BEFORE_SPAWN, utc_iso_ms(), job_id),
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
        self,
        job_id: str,
        *,
        expect_attempt: int | None = None,
        conflicts: DuplicateRule | None = None,
    ) -> dict[str, Any]:
        """Reset a terminal row back to 'queued' with attempt+1.

        The terminal-status check and the transition are ONE BEGIN IMMEDIATE,
        so two concurrent re-enqueues cannot both pass the check and stack two
        attempts (and, once the supervisor claims them, two workers) onto one
        row. ``expect_attempt`` makes the swap conditional on the exact row
        the caller inspected. ``conflicts`` (see ``enqueue_unless_active``) is
        asked of every OTHER active row of the kind in the same transaction;
        a match raises ``JobDuplicate`` and leaves the row untouched.
        """
        existing: str | None = None
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._assert_requeueable(job_id, expect_attempt)
                existing = self._refuse_duplicate_requeue(job_id, conflicts)
                if existing is None:
                    self._requeue(job_id)
                    self._conn.execute("COMMIT")
                else:
                    self._conn.execute("ROLLBACK")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            if existing is not None:
                raise self._duplicate_error(job_id, existing)
        return self._emit(job_id)

    def resolve_and_requeue(
        self,
        job_id: str,
        *,
        expect_status: str,
        expect_attempt: int,
        resolution: str,
        resolution_error: str,
        conflicts: DuplicateRule | None = None,
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
        existing: str | None = None
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._assert_unchanged(job_id, expect_status, expect_attempt)
                existing = self._refuse_duplicate_requeue(job_id, conflicts)
                if existing is None:
                    self._conn.execute(
                        "UPDATE jobs SET status=?, error=?, finished_at=? "
                        "WHERE id=?",
                        (resolution, resolution_error, utc_iso_ms(), job_id),
                    )
                    self._requeue(job_id)
                    self._conn.execute("COMMIT")
                else:
                    self._conn.execute("ROLLBACK")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            if existing is not None:
                raise self._duplicate_error(job_id, existing)
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
        """The gate for the RUNNING half of cancel.

        The queued half never reaches here: ``cancel_queued`` settles it
        first. So anything this refuses is a row that is already terminal, or
        already on its way out through 'cancelling'.
        """
        current = str(self._row_state(job_id)["status"])
        if current != "running":
            raise JobConflict(
                f"job {job_id} is {current}; cancel settles a queued row "
                "outright and stops a running one, so there is nothing to "
                "stop here"
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

    def _require_measured_update(self, updated: sqlite3.Cursor, job_id: str) -> None:
        """The measured UPDATE must change the running row it just read."""
        if updated.rowcount != 1:
            raise RuntimeError(
                f"job {job_id} was running and the measured update matched no row"
            )

    def _insert_queued(
        self,
        job_id: str,
        kind: str,
        encoded_payload: str,
        created_at: str,
        external_ref: str | None,
    ) -> None:
        """The queued INSERT. Callers own the surrounding transaction, if any."""
        self._conn.execute(
            "INSERT INTO jobs (id, kind, payload, status, created_at, "
            "owner_pid, owner_boot_id, external_ref) "
            "VALUES (?, ?, ?, 'queued', ?, ?, ?, ?)",
            (
                job_id,
                kind,
                encoded_payload,
                created_at,
                self.owner_pid,
                self.boot_id,
                external_ref,
            ),
        )

    def _assert_no_active_id_overlap(self, kind: str, wanted: set[str]) -> None:
        """Raise ActiveIdOverlap when a non-terminal row of ``kind`` owns an id.

        Caller holds the write transaction. Every non-terminal row is read:
        a capped listing would miss an older active job and insert anyway.
        """
        if not wanted:
            return
        terminal = tuple(sorted(TERMINAL_STATUSES))
        marks = ",".join("?" * len(terminal))
        rows = self._conn.execute(
            "SELECT id, payload FROM jobs WHERE kind=? "
            f"AND status NOT IN ({marks}) ORDER BY created_at DESC",
            (kind, *terminal),
        ).fetchall()
        for row in rows:
            payload = json.loads(row["payload"])
            if not isinstance(payload, dict):
                continue
            existing = payload.get("stable_ids")
            if not isinstance(existing, list):
                continue
            if wanted.intersection(existing):
                raise ActiveIdOverlap(str(row["id"]))

    def _requeue(self, job_id: str) -> None:
        """The queued-reset UPDATE. Callers own the surrounding transaction."""
        self._conn.execute(
            "UPDATE jobs SET status='queued', progress=0, message=NULL, "
            "error=NULL, attempt=attempt+1, started_at=NULL, "
            "finished_at=NULL, owner_pid=?, owner_boot_id=?, "
            "worker_pid=NULL, worker_pgid=NULL, worker_argv=NULL, "
            "worker_started_at=NULL, stderr_tail=NULL, outcome=NULL WHERE id=?",
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
            # group_cleared stays True: there is no pgid to retry against, so
            # holding the row open would re-report the same dead end on every
            # future boot without ever learning anything new.
            return ReapResult(SPAWN_WINDOW_ERROR, group_cleared=True)
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
    outcome = job.get("outcome")
    job["outcome"] = json.loads(outcome) if outcome else None
    stderr_tail = job.get("stderr_tail")
    job["stderr_tail"] = stderr_tail.split("\n") if stderr_tail else None
    return job


def open_store(
    db_path: Path, *, boot_id: str, owner_pid: int
) -> JobStore:
    return JobStore(db_path, boot_id=boot_id, owner_pid=owner_pid)


def statuses() -> Iterable[str]:
    return STATUSES


__all__ = [
    "ACTIVE_STATUSES",
    "CANCEL_BEFORE_SPAWN",
    "FINISH_SOURCES",
    "JOBS_TOPIC",
    "LIVE_STATUSES",
    "REAP_RETRY_ERROR",
    "RESTART_ERROR",
    "SPAWN_WINDOW_ERROR",
    "STATUSES",
    "TERMINAL_STATUSES",
    "ActiveIdOverlap",
    "ActiveJob",
    "DuplicateRule",
    "JobConflict",
    "JobDuplicate",
    "JobNotFound",
    "JobStore",
    "open_store",
    "statuses",
]
