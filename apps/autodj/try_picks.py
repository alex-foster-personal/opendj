"""Transition try-er storage (TRYER-13): one row per try job, one pick per job.

A try job is one ``try_pair`` run for an (outgoing, incoming) pair: its ranked
candidates, each with the TransitionPlan JSON and the path of its render. The
person (or an agent) auditions them and picks one; the pick row keeps the chosen
plan, its sha256, the scorer version that ranked it, and the ids of the
candidates that were NOT picked, which are the local training data the
calibration round reads (ADR-NEW-transition-try-picks-table).

Tables live in the shared state.db but migrate on their own private
``transition_try_schema_meta`` counter, the play-orders pattern, so this ladder
never races the Phase 5 ``schema_meta`` one.

Concurrency: every pick or undo bumps the job's ``revision``; the HTTP layer's
etag is derived from it, so a stale ``If-Match`` is refused, never last-write-wins.

  [if] a pick names a candidate the job does not hold [then] it is refused, never stored ⛔️
  [if] a pick or undo carries a revision that is not the current one [then] it is refused ⛔️
  [if] an undo runs on a job with no pick [then] it is refused, not a silent no-op ⛔️
  [if] a pick runs on a job that already has one [then] it is refused, never replaced ⛔️
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

SCHEMA_VERSION: int = 2

# Path the schema-drift scan classifies; sync_drift_subject imports this module
# directly so the quality gate never loads apps.autodj.http (pydantic).
TRY_PICKS_SCHEMA_SOURCE = "apps/autodj/try_picks.py"

_V1: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS transition_try_jobs (
        job_id          TEXT PRIMARY KEY,
        out_id          TEXT NOT NULL,
        in_id           TEXT NOT NULL,
        n               INTEGER NOT NULL,
        context_bars    INTEGER NOT NULL,
        scorer_version  TEXT NOT NULL,
        candidates_json TEXT NOT NULL,
        revision        INTEGER NOT NULL DEFAULT 0,
        created_at      TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS transition_picks (
        job_id                TEXT PRIMARY KEY REFERENCES transition_try_jobs(job_id) ON DELETE CASCADE,
        out_id                TEXT NOT NULL,
        in_id                 TEXT NOT NULL,
        picked_candidate_id   TEXT NOT NULL,
        plan_json             TEXT NOT NULL,
        plan_sha256           TEXT NOT NULL,
        scorer_version        TEXT NOT NULL,
        rejected_ids_json     TEXT NOT NULL,
        created_at            TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_transition_picks_pair ON transition_picks(out_id, in_id)",
]

#: V2 (Tue 27 Oct 2026): the execution layer's "Use best" verdict, stored with the job so every surface
#: reads the same answer: ``best_id`` NULL means no best, and ``best_reason`` then says why.
_V2: list[str] = [
    "ALTER TABLE transition_try_jobs ADD COLUMN best_id TEXT",
    "ALTER TABLE transition_try_jobs ADD COLUMN best_reason TEXT NOT NULL DEFAULT ''",
    # A v1 job never had best computed: say so, rather than leave a null best with no reason.
    "UPDATE transition_try_jobs SET best_reason = 'no best recorded: this job was stored before best was"
    " computed; render the pair again' WHERE best_id IS NULL AND best_reason = ''",
]

_MIGRATIONS: list[list[str]] = [_V1, _V2]

TABLES: tuple[str, ...] = ("transition_try_jobs", "transition_picks", "transition_try_schema_meta")


class TryStoreError(ValueError):
    """A refused store operation, with a machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TryJob:
    job_id: str
    out_id: str
    in_id: str
    n: int
    context_bars: int
    scorer_version: str
    candidates: list[dict[str, Any]]
    revision: int
    created_at: str
    best_id: str | None
    best_reason: str


@dataclass(frozen=True)
class TryPick:
    job_id: str
    out_id: str
    in_id: str
    picked_candidate_id: str
    plan: dict[str, Any]
    plan_sha256: str
    scorer_version: str
    rejected_ids: list[str]
    created_at: str


# -----------------------------------------------------------------------------
def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def apply_try_migrations(conn: sqlite3.Connection) -> int:
    """Apply pending try-er migrations; idempotent, returns the resulting version."""
    if conn.in_transaction:
        raise RuntimeError("apply_try_migrations needs a connection with no open transaction")
    # BEGIN IMMEDIATE takes the write lock BEFORE the version is read, so two first requests
    # cannot both see version 0 and run the same ALTERs.
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS transition_try_schema_meta"
            " (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        current = int(conn.execute("SELECT COALESCE(MAX(version), 0) FROM transition_try_schema_meta").fetchone()[0])
        for step in range(current, SCHEMA_VERSION):
            for statement in _MIGRATIONS[step]:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO transition_try_schema_meta(version, applied_at) VALUES (?, ?)", (step + 1, _utc_now())
            )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return max(current, SCHEMA_VERSION)


# -----------------------------------------------------------------------------
def insert_job(conn: sqlite3.Connection, job: TryJob) -> None:
    with conn:
        conn.execute(
            "INSERT INTO transition_try_jobs(job_id, out_id, in_id, n, context_bars, scorer_version,"
            " candidates_json, revision, created_at, best_id, best_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job.job_id,
                job.out_id,
                job.in_id,
                job.n,
                job.context_bars,
                job.scorer_version,
                json.dumps(job.candidates, sort_keys=True),
                job.revision,
                job.created_at,
                job.best_id,
                job.best_reason,
            ),
        )


def get_job(conn: sqlite3.Connection, job_id: str) -> TryJob:
    row = conn.execute(
        "SELECT job_id, out_id, in_id, n, context_bars, scorer_version, candidates_json, revision, created_at,"
        " best_id, best_reason FROM transition_try_jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    if row is None:
        raise TryStoreError("job_not_found", f"no try job {job_id!r}")
    return TryJob(
        row[0],
        row[1],
        row[2],
        int(row[3]),
        int(row[4]),
        row[5],
        json.loads(row[6]),
        int(row[7]),
        row[8],
        row[9],
        row[10],
    )


def get_pick(conn: sqlite3.Connection, job_id: str) -> TryPick | None:
    row = conn.execute(
        "SELECT job_id, out_id, in_id, picked_candidate_id, plan_json, plan_sha256, scorer_version,"
        " rejected_ids_json, created_at FROM transition_picks WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    return TryPick(row[0], row[1], row[2], row[3], json.loads(row[4]), row[5], row[6], json.loads(row[7]), row[8])


def _claim_revision(conn: sqlite3.Connection, job_id: str, expected_revision: int) -> None:
    """Bump the job's revision iff it is still ``expected_revision``; inside the caller's transaction."""
    cursor = conn.execute(
        "UPDATE transition_try_jobs SET revision = revision + 1 WHERE job_id = ? AND revision = ?",
        (job_id, expected_revision),
    )
    if cursor.rowcount != 1:
        raise TryStoreError("stale_revision", f"try job {job_id!r} changed since revision {expected_revision}")


def record_pick(conn: sqlite3.Connection, job_id: str, candidate_id: str, expected_revision: int) -> TryPick:
    """Record the pick, claiming the revision FIRST inside one transaction, so a stale caller always gets
    stale_revision; a logical refusal (already picked, unknown candidate) rolls the claim back."""
    get_job(conn, job_id)  # job_not_found before anything else; jobs are never deleted
    with conn:
        _claim_revision(conn, job_id, expected_revision)
        job = get_job(conn, job_id)
        if get_pick(conn, job_id) is not None:
            # Never overwrite a pick: the replaced choice would be lost. Undo first, then pick again.
            raise TryStoreError("already_picked", f"try job {job_id!r} already has a pick; undo it first")
        chosen = next((c for c in job.candidates if c["id"] == candidate_id), None)
        if chosen is None:
            raise TryStoreError("candidate_not_found", f"try job {job_id!r} has no candidate {candidate_id!r}")
        pick = TryPick(
            job_id=job.job_id,
            out_id=job.out_id,
            in_id=job.in_id,
            picked_candidate_id=candidate_id,
            plan=chosen["plan"],
            plan_sha256=chosen["plan_sha256"],
            scorer_version=chosen["scorer_version"],
            rejected_ids=[c["id"] for c in job.candidates if c["id"] != candidate_id],
            created_at=_utc_now(),
        )
        conn.execute(
            "INSERT INTO transition_picks(job_id, out_id, in_id, picked_candidate_id, plan_json,"
            " plan_sha256, scorer_version, rejected_ids_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                pick.job_id,
                pick.out_id,
                pick.in_id,
                pick.picked_candidate_id,
                json.dumps(pick.plan, sort_keys=True),
                pick.plan_sha256,
                pick.scorer_version,
                json.dumps(pick.rejected_ids),
                pick.created_at,
            ),
        )
    return pick


def undo_pick(conn: sqlite3.Connection, job_id: str, expected_revision: int) -> TryPick:
    """Remove the pick, claiming the revision first (as :func:`record_pick`); no_pick rolls the claim back."""
    get_job(conn, job_id)
    with conn:
        _claim_revision(conn, job_id, expected_revision)
        previous = get_pick(conn, job_id)
        if previous is None:
            raise TryStoreError("no_pick", f"try job {job_id!r} has no pick to undo")
        conn.execute("DELETE FROM transition_picks WHERE job_id = ?", (job_id,))
    return previous
