"""The CloudStems monthly track meter for a hosted hub (CSPAY-08).

CloudStems is sold as **200 tracks a month** (PRICE-01: paid features are
metered server-side). Until this module existed the hub gated *access* to
CloudStems (CSPAY-05) but never counted what an owner took, so one paying
owner could fetch an unbounded number of stem bundles in a month.

What is counted, and why that way
---------------------------------
One row per (owner, UTC month, ``stable_id``). Re-presigning a track the
owner already fetched this month is NOT a second track: a page reload, a
retry, or a second machine re-downloading the same bundle must not consume
quota. The bundle is "made" once, at the first presign of that stable_id for
that owner in that month; everything after it is the same track.

The month key is UTC (``%Y-%m``), derived from a caller-supplied ``now`` so a
test can move the clock without patching ``datetime``. A new month starts at
zero: the cap is a monthly rate, not a lifetime total.

The cap is REQUIRED configuration
---------------------------------
``MDT_CLOUDSTEMS_MONTHLY_CAP`` has NO default. A hidden default is how a
hosted hub silently meters at the wrong number: a missing value must refuse
the presign with 503 ``CLOUDSTEMS_MONTHLY_CAP_UNSET``, not fall back to 200
and disagree with what the price sheet says. That refusal is per request, not
at startup: ``apps.sync_hub.hosted_config`` does not read this variable, so a
hub with no cap configured still starts and answers 503 on the first presign.
A value that is not a whole number >= 1 is refused for the same reason.

Self-hosted hubs never reach this module: the caller filters on
``entitlement_gate.is_hosted`` first, so an operator's own daemon still has
no meter, no table and no cap file.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime

from apps.shared.state import db as state_db

#: Required on a hosted hub; seconds-old default is exactly the bug this
#: module refuses to repeat.
CAP_ENV: str = "MDT_CLOUDSTEMS_MONTHLY_CAP"

#: 429: the owner has used every track this month. Distinct from a plan
#: refusal -- the plan is fine, the month's allowance is gone, and it resets.
CAP_EXCEEDED_CODE: str = "CLOUDSTEMS_MONTHLY_CAP_REACHED"
#: 503: hosted hub is metering but nobody configured the number. Never defaulted.
CAP_UNSET_CODE: str = "CLOUDSTEMS_MONTHLY_CAP_UNSET"

USAGE_TABLE: str = "stem_bundle_usage"


class StemCapConfigError(RuntimeError):
    """Hosted hubs meter CloudStems; the cap setting is missing or unreadable."""


class StemCapExceeded(RuntimeError):
    """The owner's month is spent. Carries the numbers a refusal should name."""

    def __init__(self, *, month: str, used: int, cap: int) -> None:
        super().__init__(
            f"CloudStems monthly cap reached for {month}: {used} of {cap} tracks"
        )
        self.month = month
        self.used = used
        self.cap = cap


def parse_cap(env: Mapping[str, str]) -> int:
    """Read the cap strictly, or raise naming the setting. No default."""
    raw = env.get(CAP_ENV, "").strip()
    if not raw:
        raise StemCapConfigError(
            f"{CAP_ENV} is unset. A hosted hub meters CloudStems and will not "
            "guess the monthly track allowance; set it to the number the price "
            "sheet sells (200 today), or stop selling CloudStems."
        )
    # ``str.isdigit`` alone is not enough: Unicode digits such as "²" pass it
    # and then make ``int()`` raise ValueError, which the presign route does not
    # catch -- a malformed setting answered 500 instead of the documented 503
    # (Sentry 2310931, Devin discussion_r4312311568). A config number is ASCII
    # by definition, so anything else is the same refusal as "abc".
    if not (raw.isascii() and raw.isdigit()):
        raise StemCapConfigError(
            f"{CAP_ENV} must be a whole number >= 1, got {raw!r}."
        )
    try:
        value = int(raw)
    except ValueError as exc:
        # ``int()`` also refuses a digit string past its own length limit, so
        # an absurd-but-numeric setting still answers 503, never a 500.
        raise StemCapConfigError(
            f"{CAP_ENV} must be a whole number >= 1, got {raw!r}."
        ) from exc
    if value < 1:
        raise StemCapConfigError(
            f"{CAP_ENV} must be a whole number >= 1, got {raw!r}."
        )
    return value


def month_of(now: datetime) -> str:
    """The UTC month key a row is billed to. Naive datetimes are read as UTC."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now.astimezone(UTC).strftime("%Y-%m")


def _table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (USAGE_TABLE,)
    ).fetchone()
    return row is not None


def ensure_usage_table(conn: sqlite3.Connection) -> None:
    """Create the meter table. Idempotent, mirroring ``engine_identity_map``."""
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {USAGE_TABLE} (
            owner_sub TEXT NOT NULL,
            month TEXT NOT NULL,
            stable_id TEXT NOT NULL,
            counted_at TEXT NOT NULL,
            PRIMARY KEY (owner_sub, month, stable_id)
        )
        """
    )


def used(
    conn: sqlite3.Connection, *, owner_sub: str, month: str
) -> int:
    """Distinct bundles this owner has taken in ``month``. Read-only."""
    if not _table_exists(conn):
        return 0
    row = conn.execute(
        f"SELECT COUNT(*) FROM {USAGE_TABLE} WHERE owner_sub = ? AND month = ?",
        (owner_sub, month),
    ).fetchone()
    return int(row[0])


def check_room(
    conn: sqlite3.Connection,
    *,
    owner_sub: str,
    month: str,
    stable_id: str,
    cap: int,
) -> None:
    """Refuse a spent month BEFORE the hub does any presign work.

    A read-only pre-check, not a charge: it answers "is this request
    obviously past the cap" so the hub does not fetch the index and head
    every object for an owner who has nothing left this month. The charge
    itself is :func:`reserve`, called only after the presign succeeded, so a
    bundle the hub cannot serve never uses up quota (CORE review, P1).

    A ``stable_id`` already counted this month always passes: a retry is
    free even when the operator lowered the cap afterwards.
    """
    if cap < 1:
        raise StemCapConfigError(f"stems monthly cap must be >= 1, got {cap!r}")
    if not _table_exists(conn):
        return
    row = conn.execute(
        f"SELECT 1 FROM {USAGE_TABLE} "
        "WHERE owner_sub = ? AND month = ? AND stable_id = ?",
        (owner_sub, month, stable_id),
    ).fetchone()
    if row is not None:
        return
    total = used(conn, owner_sub=owner_sub, month=month)
    if total >= cap:
        raise StemCapExceeded(month=month, used=total, cap=cap)


def reserve(
    conn: sqlite3.Connection,
    *,
    owner_sub: str,
    month: str,
    stable_id: str,
    cap: int,
    counted_at: str,
) -> int:
    """Count one bundle for the owner, or raise when the month is spent.

    A (owner, month, stable_id) that is already counted is a no-op and returns
    the same total -- that is what makes a retry free. A NEW bundle that would
    take the owner past ``cap`` is removed again and refused, so the refused
    request leaves no row behind and a client cannot walk the count up by
    asking for bundles the hub refuses.

    The caller commits; this function never does, so it composes with a
    caller's own transaction.
    """
    if cap < 1:
        raise StemCapConfigError(f"stems monthly cap must be >= 1, got {cap!r}")
    ensure_usage_table(conn)
    cursor = conn.execute(
        f"INSERT OR IGNORE INTO {USAGE_TABLE} "
        "(owner_sub, month, stable_id, counted_at) VALUES (?, ?, ?, ?)",
        (owner_sub, month, stable_id, counted_at),
    )
    # ``rowcount`` is what separates "this call took the last track" from
    # "this stable_id was already counted and the cap has since been lowered
    # (or a second writer committed a moment ago)". Without it the rollback
    # below deletes a row this call did not insert -- an owner would lose a
    # track they had already been charged for, and a retry would look free.
    inserted = cursor.rowcount == 1
    total = used(conn, owner_sub=owner_sub, month=month)
    if not inserted:
        # A counted track stays free all month -- the module docstring's
        # "retry is free" promise. Refusing it because the operator lowered the
        # cap after it was taken would charge an owner for a track the hub
        # already served, and (before this branch) deleted their row too.
        return total
    if total > cap:
        conn.execute(
            f"DELETE FROM {USAGE_TABLE} "
            "WHERE owner_sub = ? AND month = ? AND stable_id = ?",
            (owner_sub, month, stable_id),
        )
        raise StemCapExceeded(month=month, used=total - 1, cap=cap)
    return total


def charge(
    conn: sqlite3.Connection,
    *,
    owner_sub: str,
    month: str,
    stable_id: str,
    cap: int,
    counted_at: str,
) -> int:
    """Count one bundle inside ONE write transaction (CORE review, P1).

    Production connections come from ``state_db.open_rw``, which opens with
    ``isolation_level=None``: every statement autocommits. Without this
    wrapper the INSERT and the COUNT inside :func:`reserve` are separate
    transactions, so two requests that each pass the cap check can both insert
    before either counts, and a retry of the first one then returns success on
    a row that has been deleted -- a served track left uncounted.

    ``BEGIN IMMEDIATE`` takes the write lock before the first statement, so a
    second writer waits (SQLite's busy timeout) and then sees the first one's
    row. Nothing is charged on a refusal: the INSERT is rolled back with the
    rest of the transaction, so a spent month leaves no row behind.

    The caller owns the connection, this function owns the transaction. A
    caller that already opened one keeps it -- SQLite refuses a nested BEGIN,
    and a caller's own transaction boundary is the better one to honour.
    """
    if conn.in_transaction:
        return reserve(
            conn,
            owner_sub=owner_sub,
            month=month,
            stable_id=stable_id,
            cap=cap,
            counted_at=counted_at,
        )
    # A writer that holds the lock past open_rw's busy timeout makes both the
    # BEGIN and the COMMIT raise a raw OperationalError, which the app turns
    # into a 500. That is a busy state store, not a server fault, so translate
    # it the way the rest of the app does (Codex round 3, non-blocking).
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        if state_db.is_sqlite_busy(exc):
            raise state_db.StateStoreBusyError(str(exc)) from exc
        raise
    try:
        total = reserve(
            conn,
            owner_sub=owner_sub,
            month=month,
            stable_id=stable_id,
            cap=cap,
            counted_at=counted_at,
        )
    except BaseException:  # roll back before anything escapes
        conn.rollback()
        raise
    try:
        conn.commit()
    except sqlite3.OperationalError as exc:
        # A failed rollback leaves the charge in an unknown state, so it is
        # raised (chained to the COMMIT failure) rather than hidden behind a
        # retryable busy error (Sol review of 61dd5ee997, P1).
        try:
            conn.rollback()
        except sqlite3.Error as rollback_exc:
            raise rollback_exc from exc
        if state_db.is_sqlite_busy(exc):
            raise state_db.StateStoreBusyError(str(exc)) from exc
        raise
    return total


__all__ = [
    "CAP_ENV",
    "CAP_EXCEEDED_CODE",
    "CAP_UNSET_CODE",
    "USAGE_TABLE",
    "StemCapConfigError",
    "StemCapExceeded",
    "charge",
    "check_room",
    "ensure_usage_table",
    "month_of",
    "parse_cap",
    "reserve",
    "used",
]
