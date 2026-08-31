"""Canonical sync stamps + the machine-local changelog.

Contract under test: ``specs/design_decision_08.md`` points 2 and 3, answering
round 1 findings 2 (ISO8601 string comparison is not an ordering over
instants) and 3 (a wall-clock push watermark silently drops local edits).

Acceptance criteria, one assertion block each:
- if canonical_now does not emit fixed-width microseconds in UTC, two
  timestamps at the same instant sort differently and LWW picks a loser --
  broken.
- if string comparison of two canonical stamps disagrees with instant
  comparison, the hub's ``sort_key <= stored`` test is not an ordering --
  broken.
- if parse_canonical accepts a naive or unparseable timestamp, a writer can
  put a value on the wire that sorts below every real stamp forever --
  broken.
- if stamp_and_log returns values without appending to local_changelog (or
  vice versa), the push fence and the row disagree about what changed --
  broken.
- if this module's row_pk encoding drifts from apps.sync_hub.protocol's, the
  spoke fence and the hub changelog name rows differently -- broken.
- if a connection with no data dir yields a machine id anyway, a DB restored
  onto another machine can impersonate it -- broken.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.shared.state import machine_identity as mid
from apps.shared.state import sync_stamp
from apps.sync_hub import protocol as hub_protocol

pytestmark = pytest.mark.requirement("INFRA-01")


# ----- (1) canonical format ------------------------------------------------


def test_canonical_now_is_fixed_width_utc() -> None:
    stamp = sync_stamp.canonical_now()
    assert stamp.endswith("+00:00")
    assert len(stamp) == len("2026-08-31T12:00:00.000000+00:00")
    # A whole-second instant still carries six digits, which is what makes
    # string comparison agree with instant comparison.
    exact = sync_stamp.canonical_from(datetime(2026, 8, 31, 12, tzinfo=UTC))
    assert exact == "2026-08-31T12:00:00.000000+00:00"


def test_canonical_from_rejects_naive_datetimes() -> None:
    with pytest.raises(sync_stamp.SyncStampError):
        # DTZ001 is the point of the test: a naive datetime must not stamp.
        sync_stamp.canonical_from(datetime(2026, 8, 31, 12))  # noqa: DTZ001


def test_canonical_from_converts_a_non_utc_offset() -> None:
    london = timezone(timedelta(hours=1))
    assert sync_stamp.canonical_from(
        datetime(2026, 8, 31, 11, tzinfo=london)
    ) == "2026-08-31T10:00:00.000000+00:00"


def test_canonical_strings_sort_as_instants_do() -> None:
    """Round 1 finding 2, verbatim: every one of these was a mis-ordering."""
    cases = [
        ("2026-08-30T10:00:00Z", "2026-08-30T10:00:00+00:00"),
        ("2026-08-30T10:00:00.000001+00:00", "2026-08-30T10:00:00+00:00"),
        ("2026-08-30T11:00:00+01:00", "2026-08-30T10:00:00+00:00"),
    ]
    for left, right in cases:
        raw_order = left > right
        canonical_order = sync_stamp.to_canonical(left) > sync_stamp.to_canonical(right)
        instant_order = sync_stamp.parse_canonical(left) > sync_stamp.parse_canonical(
            right
        )
        assert canonical_order == instant_order, (left, right)
        if raw_order != instant_order:
            # This pair is one of the three the review reproduced; the point
            # is that canonicalizing fixes it, not that raw ISO is unlucky.
            assert canonical_order != raw_order


def test_to_canonical_is_idempotent() -> None:
    once = sync_stamp.to_canonical("2026-08-30T10:00:00Z")
    assert sync_stamp.to_canonical(once) == once


@pytest.mark.parametrize(
    "bad",
    [
        "2026-08-30T10:00:00",  # naive: sorts below every stamped value
        "not-a-timestamp",
        "",
        "2026-13-01T00:00:00+00:00",
    ],
)
def test_parse_canonical_rejects_unusable_timestamps(bad: str) -> None:
    with pytest.raises(sync_stamp.SyncStampError):
        sync_stamp.parse_canonical(bad)


def test_parse_canonical_rejects_a_non_string() -> None:
    with pytest.raises(sync_stamp.SyncStampError):
        sync_stamp.parse_canonical(None)  # type: ignore[arg-type]


# ----- (2) row keys --------------------------------------------------------


def test_row_pk_encoding_matches_the_hub_protocol() -> None:
    """The spoke fence and the hub changelog must name rows identically."""
    for values in (
        ("trk-1",),
        ("trk-1", "bpm"),
        ("pl-1", 0),
        ("a,b", '["forged"]'),
        (None, "x"),
    ):
        assert sync_stamp.encode_row_pk(values) == hub_protocol.encode_row_pk(values)


def test_row_pk_cannot_be_forged_by_a_delimiter() -> None:
    assert sync_stamp.encode_row_pk(('a", "b',)) != sync_stamp.encode_row_pk(
        ("a", "b")
    )


# ----- (3) identity from a connection --------------------------------------


def test_data_dir_is_two_levels_up_from_the_canonical_layout(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "state" / "state.db"
    conn = state_db.open_rw(canonical)
    try:
        assert sync_stamp.data_dir_for_connection(conn) == tmp_path.resolve()
    finally:
        conn.close()


def test_a_sidecar_db_owns_its_own_directory(tmp_path: Path) -> None:
    sidecar = tmp_path / "sidecars" / "scratch.db"
    conn = state_db.open_rw(sidecar)
    try:
        assert sync_stamp.data_dir_for_connection(conn) == (
            tmp_path / "sidecars"
        ).resolve()
    finally:
        conn.close()


def test_an_in_memory_connection_has_no_identity() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        with pytest.raises(sync_stamp.SyncStampError):
            sync_stamp.data_dir_for_connection(conn)
        with pytest.raises(sync_stamp.SyncStampError):
            sync_stamp.local_machine_id(conn)
    finally:
        conn.close()


def test_ensure_local_machine_registers_once(state_conn: sqlite3.Connection,
                                             state_db_path: Path) -> None:
    first = sync_stamp.ensure_local_machine(state_conn)
    second = sync_stamp.ensure_local_machine(state_conn)
    assert first == second == mid.get_or_create_machine_id(state_db_path.parent)
    assert state_conn.execute("SELECT COUNT(*) FROM machines").fetchone()[0] == 1


# ----- (4) the choke point -------------------------------------------------


def _changelog(conn: sqlite3.Connection) -> list[tuple[object, ...]]:
    return conn.execute(
        "SELECT table_name, row_pk, updated_at, origin_device_id, received_at "
        "FROM local_changelog ORDER BY seq"
    ).fetchall()


def test_stamp_and_log_returns_and_logs_the_same_values(
    state_conn: sqlite3.Connection,
) -> None:
    stamp = sync_stamp.stamp_and_log(
        state_conn, "tracks", ("trk-1",), "machine-a",
    )
    assert stamp.origin_device_id == "machine-a"
    assert sync_stamp.to_canonical(stamp.updated_at) == stamp.updated_at
    assert _changelog(state_conn) == [
        (
            "tracks",
            sync_stamp.encode_row_pk(("trk-1",)),
            stamp.updated_at,
            "machine-a",
            stamp.updated_at,
        )
    ]


def test_stamp_and_log_canonicalizes_a_caller_supplied_now(
    state_conn: sqlite3.Connection,
) -> None:
    stamp = sync_stamp.stamp_and_log(
        state_conn, "tracks", ("trk-1",), "machine-a", now="2026-08-30T10:00:00Z",
    )
    assert stamp.updated_at == "2026-08-30T10:00:00.000000+00:00"


def test_stamp_and_log_refuses_a_naive_now(state_conn: sqlite3.Connection) -> None:
    with pytest.raises(sync_stamp.SyncStampError):
        sync_stamp.stamp_and_log(
            state_conn, "tracks", ("trk-1",), "machine-a",
            now="2026-08-30T10:00:00",
        )
    assert _changelog(state_conn) == [], "a refused stamp must log nothing"


def test_stamp_and_log_refuses_an_empty_machine_id(
    state_conn: sqlite3.Connection,
) -> None:
    with pytest.raises(sync_stamp.SyncStampError):
        sync_stamp.stamp_and_log(state_conn, "tracks", ("trk-1",), "")
    assert _changelog(state_conn) == []


def test_a_rolled_back_write_leaves_no_changelog_entry(
    state_conn: sqlite3.Connection,
) -> None:
    """The fence must never offer a row the transaction threw away."""
    state_conn.execute("SAVEPOINT probe")
    sync_stamp.stamp_and_log(state_conn, "tracks", ("trk-1",), "machine-a")
    state_conn.execute("ROLLBACK TO SAVEPOINT probe")
    state_conn.execute("RELEASE SAVEPOINT probe")
    assert _changelog(state_conn) == []


def test_changelog_seq_is_the_push_fence(state_conn: sqlite3.Connection) -> None:
    """ADR 08 point 3: the floor is a sequence, never a wall clock."""
    for index in range(3):
        sync_stamp.stamp_and_log(
            state_conn, "tracks", (f"trk-{index}",), "machine-a",
        )
    seqs = [
        row[0]
        for row in state_conn.execute("SELECT seq FROM local_changelog ORDER BY seq")
    ]
    assert seqs == [1, 2, 3]

    # A row written with a wildly skewed clock still lands above the fence,
    # which is the property the old wall-clock watermark did not have.
    sync_stamp.stamp_and_log(
        state_conn, "tracks", ("skewed",), "machine-a",
        now="1999-01-01T00:00:00+00:00",
    )
    above_floor = state_conn.execute(
        "SELECT row_pk FROM local_changelog WHERE seq > 3"
    ).fetchall()
    assert [row[0] for row in above_floor] == [sync_stamp.encode_row_pk(("skewed",))]
