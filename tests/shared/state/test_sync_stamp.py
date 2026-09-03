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

import os
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


def test_a_rewritten_machine_id_file_is_not_served_stale(tmp_path: Path) -> None:
    """Round 3 finding N8c: the cache must not outlive the file on disk.

    ``[observed]`` in the round 3 review: ``first=57853cc2 on_disk=550fbd02
    cached=57853cc2 stale=True`` after the id file was rewritten out from
    under a live process (a restore, an operator re-mint). A bare
    ``functools.cache`` kept the FIRST id it ever read for the life of the
    process; the fix keys the cache on the id file's mtime so a rewrite is
    picked up on the very next call, no restart required.
    """
    conn = state_db.open_rw(tmp_path / "state" / "state.db")
    try:
        sync_stamp.reset_machine_id_cache()
        first = sync_stamp.local_machine_id(conn)

        rewritten = "5" * 32
        assert rewritten != first
        id_path = mid.machine_id_path(tmp_path)
        id_path.unlink()
        id_path.write_text(rewritten, encoding="utf-8")
        id_path.chmod(mid.MACHINE_ID_MODE)

        assert sync_stamp.local_machine_id(conn) == rewritten, (
            "the cache kept serving the id read on the FIRST call"
        )
    finally:
        conn.close()
        sync_stamp.reset_machine_id_cache()


def test_a_rewrite_that_lands_on_the_SAME_mtime_is_still_not_served_stale(
    tmp_path: Path,
) -> None:
    """The sibling above, with the clock's help taken away.

    That test only discriminates while the rewrite happens to land on a
    different mtime, and inode timestamps come from the kernel's COARSE clock,
    which advances once per timer tick. On fast hardware the read and the
    rewrite fall inside one tick, the mtime comes back IDENTICAL, and an
    mtime-keyed cache serves the first id exactly as the bare
    ``functools.cache`` did before finding N8c was ever fixed. That is how the
    sibling failed the first time this suite ran on self-hosted CI
    (Thu 3 Sep 2026), and nothing in the suite could tell that apart from the
    machine being quick.

    So the identical mtime is arranged here rather than waited for: with the
    field the old key read held still, only a key that reads the BYTES can
    pass. Mutate ``_CachedId`` back to ``mtime_ns`` and this goes red on every
    machine, fast or slow.
    """
    conn = state_db.open_rw(tmp_path / "state" / "state.db")
    try:
        sync_stamp.reset_machine_id_cache()
        first = sync_stamp.local_machine_id(conn)

        rewritten = "5" * 32
        assert rewritten != first
        id_path = mid.machine_id_path(tmp_path)
        before = id_path.stat()
        id_path.unlink()
        id_path.write_text(rewritten, encoding="utf-8")
        id_path.chmod(mid.MACHINE_ID_MODE)
        os.utime(id_path, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert id_path.stat().st_mtime_ns == before.st_mtime_ns, (
            "fixture precondition: the mtime must be identical, or this is "
            "just the sibling test again and proves nothing extra"
        )

        assert sync_stamp.local_machine_id(conn) == rewritten, (
            "the cache read a clock instead of the file, so a rewrite inside "
            "one timer tick is invisible to it"
        )
    finally:
        conn.close()
        sync_stamp.reset_machine_id_cache()


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


# ----- (7) round 2 finding N3: a stored stamp must not brick the machine ----


def test_epoch_matches_the_protocol_sentinel() -> None:
    """Two definitions of "loses every conflict" must be one value.

    ``apps.shared`` cannot import ``apps.sync_hub``, so the sentinel is
    duplicated. Duplicated is fine; drifted is a spoke and a hub disagreeing
    about which row is older.
    """
    assert sync_stamp.EPOCH == hub_protocol.EPOCH


@pytest.mark.parametrize(
    "stored",
    [
        None,
        "2024-11-01T12:00:00",          # naive ISO8601
        "2024-11-01 12:00:00",          # SQLite CURRENT_TIMESTAMP spelling
        "not a timestamp at all",
        "",
    ],
)
def test_an_unorderable_stored_stamp_reads_as_epoch(stored: str | None) -> None:
    """Round 2 finding N3b.

    One such value anywhere in the library used to abort ``spoke_push``
    before a single row was offered, on every sync, forever, with no repair
    path. It now sorts exactly where a NULL stamp has always sorted.
    """
    assert sync_stamp.coalesce_stored_stamp(stored) == sync_stamp.EPOCH


def test_coalesce_leaves_an_orderable_stamp_alone() -> None:
    """Tolerating a broken value must not mean discarding a good one."""
    assert (
        sync_stamp.coalesce_stored_stamp("2026-08-30T10:00:00Z")
        == "2026-08-30T10:00:00.000000+00:00"
    )
    canonical = sync_stamp.canonical_now()
    assert sync_stamp.coalesce_stored_stamp(canonical) == canonical


def test_the_wire_boundary_still_refuses_what_storage_tolerates() -> None:
    """The asymmetry is the design, not an oversight.

    A legacy row on this machine is a fact to survive. The identical value
    arriving from a peer is a protocol violation to report -- storing it
    would put a row nothing can order into the sync set.
    """
    with pytest.raises(sync_stamp.SyncStampError):
        sync_stamp.parse_canonical("2024-11-01T12:00:00")


def test_r4_local_read_coalesces_but_the_same_value_off_the_wire_is_rejected() -> None:
    """Round 3/4 finding R4, the whole contract in one place.

    A locally-stored unorderable stamp must not brick a read: it coalesces to
    the comparison sentinel. The BYTE-IDENTICAL value arriving off the wire is
    still rejected. And the sentinel it coalesces to is itself unparseable, so
    it can only ever be compared, never stored back -- which is why the repair
    (:mod:`apps.shared.state.normalize_stamps`) writes ``FLOOR_STAMP``, a
    parseable year-one stamp, rather than :data:`sync_stamp.EPOCH`.
    """
    legacy = "2024-11-01 12:00:00"  # SQLite CURRENT_TIMESTAMP spelling

    # Local read: survivable, sorts where a NULL stamp sorts.
    assert sync_stamp.coalesce_stored_stamp(legacy) == sync_stamp.EPOCH

    # Same value off the wire: refused.
    with pytest.raises(sync_stamp.SyncStampError):
        sync_stamp.parse_canonical(legacy)

    # The sentinel is a comparison value only: it does not round-trip through
    # the wire parser, so nothing may store it back onto a row.
    with pytest.raises(sync_stamp.SyncStampError):
        sync_stamp.parse_canonical(sync_stamp.EPOCH)
    from apps.shared.state import normalize_stamps

    assert normalize_stamps.FLOOR_STAMP != sync_stamp.EPOCH
    assert sync_stamp.parse_canonical(normalize_stamps.FLOOR_STAMP)


def _legacy_location(
    conn: sqlite3.Connection, location_id: str, stable_id: str, *, updated_at: str
) -> None:
    """A v5-era location row: no machine_id, whatever stamp it carried."""
    conn.execute(
        "INSERT INTO track_locations(location_id, stable_id, kind, file_path, "
        "created_at, updated_at) VALUES (?, ?, 'local', ?, ?, ?)",
        (location_id, stable_id, f"/Music/{location_id}.mp3", updated_at, updated_at),
    )


def _seed_track(conn: sqlite3.Connection, stable_id: str) -> None:
    conn.execute(
        "INSERT INTO tracks(stable_id, stable_id_tier, title, created_at, "
        "updated_at) VALUES (?, 'inferred', 'legacy', ?, ?)",
        (stable_id, "2024-11-01T12:00:00+00:00", "2024-11-01T12:00:00+00:00"),
    )


def test_the_backfill_survives_a_legacy_naive_stamp(
    state_conn: sqlite3.Connection,
) -> None:
    """Round 2 finding N3a/N3b together, as the probe hit them.

    The observed failure was:

      [observed] f1 first open -> SyncStampError: '2024-11-01T12:00:00' has
                                  no UTC offset
      [observed] f1 second open OK; rows(claimed?) = all three
      [observed] f1 local_changelog track_locations entries = 2 of 3

    All three rows claimed, two logged, and because the retry predicate is
    ``machine_id IS NULL`` the second open was a silent no-op reporting
    success over the gap. The unlogged row was invisible to the push fence
    forever.
    """
    _seed_track(state_conn, "e" * 40)
    _legacy_location(state_conn, "good", "e" * 40, updated_at="2024-11-01T12:00:00+00:00")
    _legacy_location(state_conn, "bad", "e" * 40, updated_at="2024-11-01T12:00:00")
    _legacy_location(state_conn, "also", "e" * 40, updated_at="2024-11-01 12:00:00")

    claimed = sync_stamp.backfill_local_machine_id(state_conn)
    assert claimed == 3

    assert state_conn.execute(
        "SELECT COUNT(*) FROM track_locations WHERE machine_id IS NULL"
    ).fetchone()[0] == 0, "a claimed row with no machine_id is invisible locally"

    logged = state_conn.execute(
        "SELECT row_pk, updated_at FROM local_changelog "
        "WHERE table_name = 'track_locations' ORDER BY seq"
    ).fetchall()
    assert len(logged) == 3, (
        "every claimed row must be logged or the push fence never offers it"
    )
    by_pk = {row[0]: row[1] for row in logged}
    assert by_pk[sync_stamp.encode_row_pk(("bad",))] == sync_stamp.EPOCH
    assert by_pk[sync_stamp.encode_row_pk(("also",))] == sync_stamp.EPOCH
    assert by_pk[sync_stamp.encode_row_pk(("good",))] == (
        "2024-11-01T12:00:00.000000+00:00"
    )


def test_a_failed_backfill_claims_nothing_and_the_retry_retries(
    state_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 2 finding N3a: half a claim is worse than no claim.

    The failure is injected at the changelog append, which is exactly where
    the round 2 probe's ``to_canonical`` raised: after the row UPDATE had
    already committed on an autocommit connection.
    """
    _seed_track(state_conn, "f" * 40)
    for index in range(3):
        _legacy_location(
            state_conn, f"loc-{index}", "f" * 40,
            updated_at="2024-11-01T12:00:00+00:00",
        )

    calls = {"n": 0}
    real_encode = sync_stamp.encode_row_pk

    def _explode_on_the_second_row(values):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated failure mid-backfill")
        return real_encode(values)

    monkeypatch.setattr(sync_stamp, "encode_row_pk", _explode_on_the_second_row)
    with pytest.raises(RuntimeError, match="simulated failure"):
        sync_stamp.backfill_local_machine_id(state_conn)

    assert state_conn.execute(
        "SELECT COUNT(*) FROM track_locations WHERE machine_id IS NULL"
    ).fetchone()[0] == 3, (
        "a failed backfill must claim NOTHING; a partial claim makes the "
        "retry a no-op and strands the unlogged rows forever"
    )
    assert state_conn.execute(
        "SELECT COUNT(*) FROM local_changelog"
    ).fetchone()[0] == 0

    monkeypatch.setattr(sync_stamp, "encode_row_pk", real_encode)
    assert sync_stamp.backfill_local_machine_id(state_conn) == 3
    assert state_conn.execute(
        "SELECT COUNT(*) FROM local_changelog WHERE table_name = 'track_locations'"
    ).fetchone()[0] == 3


def test_stamped_transaction_rolls_back_a_partial_unit(
    state_conn: sqlite3.Connection,
) -> None:
    """The row and its changelog entry commit together, or neither does."""
    _seed_track(state_conn, "g" * 40)
    with pytest.raises(RuntimeError, match="boom"):
        with sync_stamp.stamped_transaction(state_conn):
            sync_stamp.stamp_and_log(
                state_conn, "tracks", ("g" * 40,), "machine-a",
            )
            raise RuntimeError("boom")
    assert state_conn.execute(
        "SELECT COUNT(*) FROM local_changelog"
    ).fetchone()[0] == 0
    assert not state_conn.in_transaction


def test_stamped_transaction_nests_without_committing_its_caller(
    state_conn: sqlite3.Connection,
) -> None:
    """A writer already in a transaction keeps ONE unit, not two."""
    _seed_track(state_conn, "h" * 40)
    state_conn.execute("BEGIN IMMEDIATE")
    sync_stamp.stamp_and_log(state_conn, "tracks", ("outer",), "machine-a")
    with sync_stamp.stamped_transaction(state_conn):
        sync_stamp.stamp_and_log(state_conn, "tracks", ("inner",), "machine-a")
    assert state_conn.in_transaction, (
        "a nested unit must not commit the caller's open transaction"
    )
    state_conn.execute("ROLLBACK")
    assert state_conn.execute(
        "SELECT COUNT(*) FROM local_changelog"
    ).fetchone()[0] == 0
