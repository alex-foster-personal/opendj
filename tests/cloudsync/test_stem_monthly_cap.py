"""The CloudStems monthly track meter (CSPAY-07), at its boundaries.

A real sqlite DB, the real meter: cap-1, cap, cap+1 and the month rollover,
plus the two properties that make the count honest -- a retried bundle is not
a second track, and a REFUSED bundle leaves no row behind.

The requirement id (CSPAY-08) is declared in ``.planning/REQUIREMENTS.md`` and
regenerated into ``reqs.json`` by this change, so the marker below is a real
claim rather than an orphan (an orphan marker reddens
``test_no_orphan_requirement_markers`` and the marker ratchet gate).

- [if] a retried bundle counts as a second track [then] a page reload spends an owner's month, [else stop].
- [if] a refused bundle leaves its row behind, or a lowered cap deletes a counted row [then] the owner's count lies, [else stop].
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import Request

from apps.shared.state import db as state_db
from apps.sync_hub import stem_meter

pytestmark = pytest.mark.requirement("CSPAY-08")

OWNER: str = "owner-sub-1"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _take(conn: sqlite3.Connection, *, stable_id: str, cap: int, month: str = "2026-10") -> int:
    used = stem_meter.reserve(
        conn,
        owner_sub=OWNER,
        month=month,
        stable_id=stable_id,
        cap=cap,
        counted_at="2026-10-18T00:00:00Z",
    )
    conn.commit()
    return used


def _fill(conn: sqlite3.Connection, count: int, *, cap: int, month: str = "2026-10") -> None:
    for index in range(count):
        _take(conn, stable_id=f"track-{index:04d}", cap=cap, month=month)


def test_month_of_is_utc_and_naive_datetimes_are_utc() -> None:
    naive = datetime(2026, 10, 31, 23, 59, 59)  # noqa: DTZ001 - a naive datetime is the case under test
    aware = datetime(2026, 11, 1, 0, 0, 1, tzinfo=UTC)
    assert stem_meter.month_of(naive) == "2026-10"
    assert stem_meter.month_of(aware) == "2026-11"


def test_parse_cap_has_no_default_and_refuses_non_numbers() -> None:
    assert stem_meter.parse_cap({stem_meter.CAP_ENV: "200"}) == 200
    for bad in ("", "0", "-1", "abc", "20.5", "  "):
        with pytest.raises(stem_meter.StemCapConfigError):
            stem_meter.parse_cap({stem_meter.CAP_ENV: bad})
    with pytest.raises(stem_meter.StemCapConfigError):
        stem_meter.parse_cap({})


def test_cap_minus_one_is_allowed() -> None:
    conn = _conn()
    _fill(conn, 4, cap=5)
    assert _take(conn, stable_id="track-0004", cap=5) == 5


def test_exactly_cap_is_allowed() -> None:
    conn = _conn()
    _fill(conn, 4, cap=5)
    _take(conn, stable_id="track-0004", cap=5)
    assert stem_meter.used(conn, owner_sub=OWNER, month="2026-10") == 5


def test_cap_plus_one_is_refused_and_leaves_no_row() -> None:
    conn = _conn()
    _fill(conn, 5, cap=5)
    with pytest.raises(stem_meter.StemCapExceeded) as raised:
        _take(conn, stable_id="track-0005", cap=5)
    conn.commit()
    assert (raised.value.used, raised.value.cap, raised.value.month) == (5, 5, "2026-10")
    assert stem_meter.used(conn, owner_sub=OWNER, month="2026-10") == 5
    rows = conn.execute(
        f"SELECT COUNT(*) FROM {stem_meter.USAGE_TABLE} WHERE stable_id = 'track-0005'"
    ).fetchone()[0]
    assert rows == 0


def test_a_retried_bundle_is_not_a_second_track() -> None:
    conn = _conn()
    assert _take(conn, stable_id="track-0000", cap=5) == 1
    assert _take(conn, stable_id="track-0000", cap=5) == 1


def test_a_lowered_cap_keeps_a_counted_track_free() -> None:
    """A retry of a counted track is free whatever the cap now says.

    Reproduced the original defect: two tracks at cap 2, the cap lowered to 1,
    and a re-presign of ``track-0000`` was refused AND deleted that row, which
    refunded a track the owner was already charged for and left the count
    measured against a set that had silently shrunk.
    """
    conn = _conn()
    _fill(conn, 2, cap=2)
    assert _take(conn, stable_id="track-0000", cap=1) == 2
    assert stem_meter.used(conn, owner_sub=OWNER, month="2026-10") == 2
    # The cap still refuses a NEW track at the lowered number.
    with pytest.raises(stem_meter.StemCapExceeded) as raised:
        _take(conn, stable_id="track-0009", cap=1)
    assert (raised.value.used, raised.value.cap) == (2, 1)
    assert stem_meter.used(conn, owner_sub=OWNER, month="2026-10") == 2


def test_a_new_month_starts_at_zero() -> None:
    conn = _conn()
    _fill(conn, 5, cap=5)
    assert _take(conn, stable_id="track-0000", cap=5, month="2026-11") == 1
    assert stem_meter.used(conn, owner_sub=OWNER, month="2026-10") == 5
    assert stem_meter.used(conn, owner_sub=OWNER, month="2026-11") == 1


def test_the_cap_is_per_owner() -> None:
    conn = _conn()
    _fill(conn, 5, cap=5)
    other = stem_meter.reserve(
        conn,
        owner_sub="owner-sub-2",
        month="2026-10",
        stable_id="track-0000",
        cap=5,
        counted_at="2026-10-18T00:00:00Z",
    )
    assert other == 1


def test_parse_cap_refuses_unicode_digits_and_oversized_numbers() -> None:
    """A numeric-looking setting answers 503, never a 500 (Sentry 2310931).

    ``str.isdigit`` is true for "²", which then makes ``int()`` raise
    ValueError: the presign route catches only StemCapConfigError, so before
    this the malformed setting was a 500. An oversized digit string hits
    CPython's own int-from-str length limit and raises the same ValueError.
    """
    for raw in ("\u00b2", "\u00b2\u2070\u2070", "9" * 5_000):
        with pytest.raises(stem_meter.StemCapConfigError):
            stem_meter.parse_cap({stem_meter.CAP_ENV: raw})
    assert stem_meter.parse_cap({stem_meter.CAP_ENV: "200"}) == 200
    assert stem_meter.parse_cap({stem_meter.CAP_ENV: " 200 "}) == 200


STAMP: str = "2026-10-19T00:00:00Z"


def _file_conn(path: Path, *, busy_timeout_ms: int = 30_000) -> sqlite3.Connection:
    """A connection shaped like production: autocommit, as open_rw opens it."""
    conn = sqlite3.connect(path, isolation_level=None, timeout=busy_timeout_ms / 1_000)
    conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
    stem_meter.ensure_usage_table(conn)
    return conn


def _charge(conn: sqlite3.Connection, stable_id: str, *, cap: int) -> int:
    return stem_meter.charge(
        conn,
        owner_sub=OWNER,
        month="2026-10",
        stable_id=stable_id,
        cap=cap,
        counted_at=STAMP,
    )


def test_charge_holds_the_write_lock_across_the_count(tmp_path: Path) -> None:
    """BEGIN IMMEDIATE is taken BEFORE the insert and the count (Codex P1).

    Proven on real SQLite with two connections: while one charge is open on a
    spent month, a second writer cannot start its own transaction at all, so
    it cannot count a row that is about to be rolled back.
    """
    path = tmp_path / "state.db"
    first = _file_conn(path)
    second = _file_conn(path, busy_timeout_ms=50)
    first.execute("BEGIN IMMEDIATE")
    # A held lock is a BUSY state store, not a 500 (Codex round 3): charge()
    # translates sqlite's OperationalError the same way writer_common does.
    with pytest.raises(state_db.StateStoreBusyError, match="locked"):
        _charge(second, "track-0001", cap=1)
    first.rollback()
    assert _charge(second, "track-0001", cap=1) == 1


class _BusyOnCommit:
    """An autocommit-shaped connection whose COMMIT loses the lock."""

    in_transaction = False

    def __init__(self, inner: sqlite3.Connection) -> None:
        self._inner = inner

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        return self._inner.execute(sql, *args)

    def rollback(self) -> None:
        self._inner.rollback()

    def commit(self) -> None:
        raise sqlite3.OperationalError("database is locked")


def test_a_commit_that_loses_the_lock_is_a_busy_store_not_a_500(tmp_path: Path) -> None:
    """The other end of the transaction: COMMIT can also hit a busy lock.

    sqlite raises SQLITE_BUSY on COMMIT when another writer took the lock
    between BEGIN and the last statement. Before this the raw
    OperationalError escaped ``charge`` and the route answered 500; the
    translation has to cover both ends, not just BEGIN.
    """
    inner = _file_conn(tmp_path / "state.db")
    with pytest.raises(state_db.StateStoreBusyError, match="locked"):
        _charge(_BusyOnCommit(inner), "track-0000", cap=5)  # type: ignore[arg-type]
    # Nothing was counted and the transaction was rolled back, so a retry is
    # still the first track of the month.
    assert inner.in_transaction is False
    assert stem_meter.used(inner, owner_sub=OWNER, month="2026-10") == 0


class _RollbackAlsoFails(_BusyOnCommit):
    """COMMIT loses the lock and the cleanup rollback fails too."""

    def rollback(self) -> None:
        raise sqlite3.OperationalError("disk I/O error")


def test_a_failed_rollback_after_commit_is_not_hidden_as_busy(tmp_path: Path) -> None:
    """[if] a failed cleanup rollback reads as a retryable busy store [then] broken.

    Sol on 61dd5ee997 (P1): the rollback was suppressed, so the caller saw
    StateStoreBusyError even though the charge's state was unknown. The
    rollback failure now escapes, chained to the COMMIT failure.
    """
    inner = _file_conn(tmp_path / "state.db")
    with pytest.raises(sqlite3.OperationalError, match="disk I/O") as raised:
        _charge(_RollbackAlsoFails(inner), "track-0000", cap=5)  # type: ignore[arg-type]
    assert not isinstance(raised.value, state_db.StateStoreBusyError)
    assert isinstance(raised.value.__cause__, sqlite3.OperationalError)
    assert "locked" in str(raised.value.__cause__)
    inner.rollback()


def test_a_refused_charge_rolls_back_only_its_own_row(tmp_path: Path) -> None:
    """A 429 leaves no row behind and does not refund the served track."""
    conn = _file_conn(tmp_path / "state.db")
    assert _charge(conn, "track-0000", cap=1) == 1
    with pytest.raises(stem_meter.StemCapExceeded) as raised:
        _charge(conn, "track-0001", cap=1)
    assert (raised.value.used, raised.value.cap) == (1, 1)
    assert conn.in_transaction is False
    assert stem_meter.used(conn, owner_sub=OWNER, month="2026-10") == 1
    rows = conn.execute(f"SELECT stable_id FROM {stem_meter.USAGE_TABLE}").fetchall()
    assert rows == [("track-0000",)]


def _charge_once(
    path: Path,
    stable_id: str,
    *,
    barrier: threading.Barrier,
    outcomes: list[tuple[str, str]],
    lock: threading.Lock,
) -> None:
    """One racing writer: charge ``stable_id`` on its own autocommit connection."""
    conn = _file_conn(path)
    try:
        barrier.wait(timeout=10)
        total = _charge(conn, stable_id, cap=1)
    except stem_meter.StemCapExceeded:
        with lock:
            outcomes.append(("refused", stable_id))
    else:
        with lock:
            outcomes.append(("counted", stable_id))
        assert total == 1, f"a cap of 1 counted {total} tracks"
    finally:
        conn.close()


def test_two_connections_cannot_leave_a_served_track_uncounted(
    tmp_path: Path,
) -> None:
    """The interleave Codex named, on two real connections (P1).

    On autocommit connections each writer's INSERT commits before either
    COUNTS, so both can be inside the cap and the loser's refusal deletes a
    row the other request was already served. ``charge`` serializes them: one
    request gets the track, the other is refused, and the table holds exactly
    the id that was served -- never a success on a deleted row.
    """
    rounds = 6
    for round_index in range(rounds):
        path = tmp_path / f"state-{round_index}.db"
        _file_conn(path).close()
        outcomes: list[tuple[str, str]] = []
        barrier = threading.Barrier(2)
        lock = threading.Lock()
        threads = [
            threading.Thread(
                target=_charge_once,
                args=(path, stable_id),
                kwargs={"barrier": barrier, "outcomes": outcomes, "lock": lock},
            )
            for stable_id in ("track-a", "track-b")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        counted = [stable_id for kind, stable_id in outcomes if kind == "counted"]
        assert len(counted) == 1, f"round {round_index}: counted={counted} outcomes={outcomes}"
        final = _file_conn(path)
        assert stem_meter.used(final, owner_sub=OWNER, month="2026-10") == 1
        rows = final.execute(
            f"SELECT stable_id FROM {stem_meter.USAGE_TABLE} "
            "WHERE owner_sub = ? AND month = ?",
            (OWNER, "2026-10"),
        ).fetchall()
        assert [row[0] for row in rows] == counted, f"round {round_index}: {rows}"
        # The served track stays free: a retry is not charged again.
        assert _charge(final, counted[0], cap=1) == 1
        final.close()


def test_charge_begins_immediate_before_inserting_and_counts_inside_it(
    tmp_path: Path,
) -> None:
    """The transaction boundary, read off real SQLite's own statement trace.

    The threaded test above can pass by timing; this one cannot. On an
    autocommit connection (which is what production hands ``_charge_bundle``)
    the INSERT must not commit before the COUNT, or two writers can each insert
    before either counts. ``BEGIN IMMEDIATE`` first, ``COMMIT`` last.
    """
    conn = _file_conn(tmp_path / "state.db")
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    assert _charge(conn, "track-0000", cap=1) == 1
    conn.set_trace_callback(None)
    upper = [" ".join(statement.split()).upper() for statement in statements]
    assert any(statement.startswith("BEGIN IMMEDIATE") for statement in upper), statements
    begin = next(i for i, statement in enumerate(upper) if statement.startswith("BEGIN IMMEDIATE"))
    insert = next(i for i, statement in enumerate(upper) if statement.startswith("INSERT"))
    count = next(i for i, statement in enumerate(upper) if "COUNT(" in statement)
    commit = next(i for i, statement in enumerate(upper) if statement.startswith("COMMIT"))
    assert begin < insert < count < commit, statements


def test_the_charge_derives_its_month_from_the_charge_instant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a presign crossing midnight charges the wrong month [then] broken.

    Codex round 6 (non-blocking): the pre-check captured the month before the
    index/HEAD/presign work, so a request that started at 23:59 UTC and
    finished after midnight charged October's allowance with a November
    ``counted_at``. The month is now derived next to the timestamp.
    """
    from apps.sync_hub import service_stem_assets

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> _FixedDatetime:
            return cls(2026, 12, 31, 23, 59, 59, tzinfo=UTC)

    conn = _file_conn(tmp_path / "state.db")

    class _Ctx:
        def __enter__(self) -> sqlite3.Connection:
            return conn

        def __exit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(service_stem_assets, "datetime", _FixedDatetime)
    monkeypatch.setattr(service_stem_assets, "_hub_conn", lambda _request: _Ctx())
    # _hub_conn is patched above, so the request is never read.
    service_stem_assets._charge_bundle(
        cast(Request, None), owner_sub=OWNER, cap=5, stable_id="track-0000"
    )
    row = conn.execute(
        f"SELECT month, counted_at FROM {stem_meter.USAGE_TABLE} WHERE stable_id = ?",
        ("track-0000",),
    ).fetchone()
    assert row is not None
    assert row[0] == "2026-12"
    assert row[1].startswith("2026-12-31")
    assert stem_meter.used(conn, owner_sub=OWNER, month="2026-12") == 1
