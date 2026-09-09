"""Hub sync against the REAL library: convergence, cost, tombstones, relinks.

Contract under test: ``specs/design_decision_04.md`` over the v7 schema of
``specs/design_decision_05.md``, same as ``test_hub_sync.py`` -- but driven by
a deterministic slice of the production 9,986-track library instead of rows a
fixture invented. That difference is the whole point of this module: synthetic
rows come from one writer, in one era, in one timestamp spelling, and agree
with the protocol by construction. Real rows do none of that, and the first
thing this tier found when it was pointed at the library was a class of row
that stopped ``spoke_push`` dead (see the last two tests here). The whole tier SKIPS
where the library is absent; see ``tests/cloudsync/conftest.py``.

Acceptance criteria, one test each:

- if a real subset does not leave all three machines byte-identical per table
  after one round robin, the engine converges on fixtures and not on data --
  broken.
- if a second sync re-offers the library, the fence does not hold at a scale
  where a full re-offer and a correct fence look different -- broken.
- if a first sync of ~3,300 real rows stops batching, or its payload or wall
  time jumps by an order of magnitude, a cost regression has landed that no
  synthetic fixture is large enough to notice -- broken.
- if a real 45-track playlist reordered on one spoke does not arrive on the
  other as that exact list, whole-playlist replacement (ADR 04 c5) does not
  survive real membership sizes -- broken.
- if a tombstoned real track is hard-deleted anywhere, or comes back on the
  next sync, soft delete does not hold on real rows -- broken.
- if re-minting 250 ``location_id`` values (what a relink or a re-ingest does)
  leaves duplicate natural keys, or grows the table, the natural-key
  resolution degrades exactly where round 1 finding 1 lived -- broken.
- if the real library needed no stamp repair, this tier's premise is wrong and
  the repair pass is untested against the data it exists for -- broken.

Round 5 moved the quarantine tests out to
:mod:`tests.cloudsync.test_real_library_quarantine` (quality-gate file_size
ratchet); the stamp-repair PREMISE assertion stays here because it is this
tier's own premise, not a quarantine behaviour.

Numbers this module measures are printed. Run with ``-s`` to see them.
"""
from __future__ import annotations

import math
import sqlite3
import time
from collections.abc import Iterator

import pytest

from apps.shared.state import sync_stamp
from apps.sync_hub import client, protocol

from .real_library import (
    PreparedLibrary,
    RealLibraryFleet,
    SeededSpoke,
    largest_playlist,
    members_of,
    table_counts,
)
from .test_hub_sync import _open

pytestmark = pytest.mark.requirement("CAT-04")

#: The transport is a ``TestClient``, not a URL; ``run_sync`` still wants one.
HUB_URL = "http://hub.invalid"

#: Generous by an order of magnitude on purpose. Observed on this Mac for the
#: 500-track subset (3,267 rows): 471 request bytes and 455 response bytes per
#: row, 0.51 s wall. These bounds catch a payload or a cost that changed SHAPE
#: (a full re-offer every sync, an unbatched body, an O(n^2) apply), not a
#: slower laptop.
MAX_FIRST_SYNC_BYTES_PER_ROW: int = 2_000
MAX_FIRST_SYNC_SECONDS: float = 60.0

#: ``location_id`` values re-minted in the relink test. Above 200 because the
#: acceptance criterion is 200+ rows resolving on the natural key.
RELINKED_LOCATIONS: int = 250

#: Prefix for those re-minted ids. Deterministic, so a failure names the exact
#: row, and unmistakably not a uuid4 hex, so a stray one is obvious in a dump.
RELINK_ID_PREFIX: str = "relink"

#: Subset size for tests whose behavior is per-row, where 500 real tracks buy
#: nothing over 120 and cost a second of fixture build.
SMALL_SUBSET_TRACKS: int = 120


@pytest.fixture(autouse=True)
def _no_hub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``MDT_IS_HUB`` from the developer's shell must not steer these tests."""
    monkeypatch.delenv("MDT_IS_HUB", raising=False)


# ----- driving the fleet -------------------------------------------------------


def _sync(fleet: RealLibraryFleet, spoke: SeededSpoke) -> client.SyncResult:
    """One full sync for ``spoke`` over the fleet's counting transport."""
    return client.run_sync(
        spoke.data_dir,
        HUB_URL,
        transport=fleet.transport,
        name=spoke.data_dir.name,
    )


def _round_robin(fleet: RealLibraryFleet) -> None:
    """Sync until every machine has seen every other machine's rows.

    A, then B, then A again: B's first sync cannot deliver rows A has not
    pushed yet, and A's own rows come back to A only after B has pushed. Two
    spokes therefore need three syncs to converge, not two.
    """
    _sync(fleet, fleet.spoke_a)
    _sync(fleet, fleet.spoke_b)
    _sync(fleet, fleet.spoke_a)


def _connections(
    fleet: RealLibraryFleet,
) -> Iterator[tuple[str, sqlite3.Connection]]:
    """Each machine's DB in turn, labeled, closed on the way out."""
    for label, data_dir in (
        ("hub", fleet.hub_dir),
        ("spoke-a", fleet.spoke_a.data_dir),
        ("spoke-b", fleet.spoke_b.data_dir),
    ):
        conn = _open(data_dir)
        try:
            yield label, conn
        finally:
            conn.close()


def _fleet_digests(fleet: RealLibraryFleet) -> dict[str, protocol.SyncDigest]:
    return {
        label: protocol.sync_digest(conn) for label, conn in _connections(fleet)
    }


def _assert_converged(fleet: RealLibraryFleet) -> dict[str, protocol.SyncDigest]:
    """Per-table digest equality across hub and both spokes.

    Per TABLE, not just the rollup: a rollup mismatch says the fleet diverged,
    a table mismatch says WHERE, and that difference is most of the cost of
    debugging a failure over 3,000 real rows.
    """
    digests = _fleet_digests(fleet)
    for table in protocol.DIGEST_TABLES:
        values = {label: digest.tables[table] for label, digest in digests.items()}
        assert len(set(values.values())) == 1, f"{table} diverged: {values}"
    assert len({digest.overall for digest in digests.values()}) == 1
    return digests


# ----- writing to a spoke the way a real writer does ---------------------------


def _replace_members(
    conn: sqlite3.Connection,
    playlist_id: str,
    stable_ids: tuple[str, ...],
    machine_id: str,
) -> str:
    """Rewrite one playlist's membership; return the stamp it was written at.

    Stamping the PLAYLIST row is the load-bearing half (ADR 04 c5): membership
    never travels on its own, so an edit that only touched the membership rows
    would never reach a peer.
    """
    stamp = sync_stamp.stamp_and_log(conn, "playlists", (playlist_id,), machine_id)
    conn.execute(
        "DELETE FROM playlist_memberships WHERE playlist_id = ?", (playlist_id,)
    )
    for position, stable_id in enumerate(stable_ids):
        conn.execute(
            "INSERT INTO playlist_memberships("
            "playlist_id, stable_id, position, updated_at, origin_device_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (playlist_id, stable_id, position, stamp.updated_at, machine_id),
        )
    conn.execute(
        "UPDATE playlists SET updated_at = ?, origin_device_id = ? "
        "WHERE playlist_id = ?",
        (stamp.updated_at, machine_id, playlist_id),
    )
    return stamp.updated_at


def _relink(
    conn: sqlite3.Connection, old_ids: list[str], machine_id: str
) -> list[str]:
    """Give each row a new ``location_id``, stamped and logged like a writer."""
    new_ids: list[str] = []
    conn.execute("BEGIN")
    try:
        for index, old_id in enumerate(old_ids):
            new_id = f"{RELINK_ID_PREFIX}{index:026d}"
            stamp = sync_stamp.stamp_and_log(
                conn, "track_locations", (new_id,), machine_id
            )
            conn.execute(
                "UPDATE track_locations SET location_id = ?, updated_at = ?, "
                "origin_device_id = ? WHERE location_id = ?",
                (new_id, stamp.updated_at, machine_id, old_id),
            )
            new_ids.append(new_id)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return new_ids


def _duplicate_natural_keys(conn: sqlite3.Connection) -> int:
    """``track_locations`` natural keys held by more than one row.

    Counted over every tuple ``protocol.NATURAL_KEYS`` declares, with the same
    all-columns-non-NULL applicability rule the engine uses, so this cannot
    drift from what the merge resolves against.
    """
    duplicates = 0
    for columns in protocol.NATURAL_KEYS["track_locations"]:
        applicable = " AND ".join(f"{column} IS NOT NULL" for column in columns)
        listed = ", ".join(columns)
        duplicates += int(
            conn.execute(
                f"SELECT COUNT(*) FROM (SELECT {listed} FROM track_locations "
                f"WHERE {applicable} GROUP BY {listed} HAVING COUNT(*) > 1)"
            ).fetchone()[0]
        )
    return duplicates


# ----- convergence -------------------------------------------------------------


def test_a_real_subset_converges_across_the_fleet(
    real_library_hub_and_spokes: RealLibraryFleet,
) -> None:
    """Two spokes holding the same real slice converge in one round robin."""
    fleet = real_library_hub_and_spokes
    first = _sync(fleet, fleet.spoke_a)
    assert first.pushed == fleet.spoke_a.pushable_rows
    assert first.accepted == first.pushed, "the hub refused rows it had never seen"
    assert first.rejected == 0

    second = _sync(fleet, fleet.spoke_b)
    # B's domain rows are byte-identical to A's, so LWW must reject them
    # rather than rewrite the hub; only B's own track_locations are new.
    assert second.accepted == fleet.spoke_b.row_counts["track_locations"]
    assert second.rejected == second.pushed - second.accepted

    _sync(fleet, fleet.spoke_a)
    _assert_converged(fleet)

    expected_locations = (
        fleet.spoke_a.row_counts["track_locations"]
        + fleet.spoke_b.row_counts["track_locations"]
    )
    counts: dict[str, dict[str, int]] = {}
    for label, conn in _connections(fleet):
        counts[label] = table_counts(conn)
    for label, count in counts.items():
        assert count["tracks"] == fleet.subset_tracks, label
        # One track_locations row per machine per file: the per-machine
        # semantics of ADR 08 point 1, at real scale.
        assert count["track_locations"] == expected_locations, label
    print(f"[real-library] converged row counts: {counts['hub']}")


def test_a_second_sync_of_a_real_library_offers_nothing(
    real_library_hub_and_spokes: RealLibraryFleet,
) -> None:
    """The watermark holds at real scale: no full re-offer on sync two.

    The synthetic twin of this (``test_resync_is_a_no_op``) has one row, so a
    full re-offer and a correct fence look identical in its counters. At 3,000
    rows they do not.
    """
    fleet = real_library_hub_and_spokes
    first = _sync(fleet, fleet.spoke_a)
    assert first.pushed == fleet.spoke_a.pushable_rows
    again = _sync(fleet, fleet.spoke_a)
    assert again.pushed == 0, "the spoke re-offered its whole library"
    assert again.applied == 0
    assert again.rounds == 1


# ----- cost --------------------------------------------------------------------


def test_first_sync_payload_and_wall_time_stay_within_bounds(
    real_library_hub_and_spokes: RealLibraryFleet,
) -> None:
    """A first sync of the real subset is batched, and its cost is bounded.

    The bounds are generous; the CONTROL is not. A payload guard that only
    checks an upper bound also passes when nothing was sent at all, so the
    accepted row count and the request count are asserted FIRST: the bytes
    weighed are then known to be the bytes of a sync that delivered the
    library.
    """
    fleet = real_library_hub_and_spokes
    transport = fleet.transport
    assert transport.requests == 0, "the fixture synced before the measurement"

    started = time.perf_counter()
    result = _sync(fleet, fleet.spoke_a)
    elapsed = time.perf_counter() - started

    rows = fleet.spoke_a.pushable_rows
    assert result.accepted == rows, "nothing to weigh: the push delivered nothing"
    expected_requests = math.ceil(rows / client.PUSH_BATCH_ROWS)
    assert result.push_requests == expected_requests, (
        f"{rows} rows went out in {result.push_requests} requests, expected "
        f"{expected_requests} at {client.PUSH_BATCH_ROWS} rows per batch"
    )
    assert result.pull_requests >= 1

    request_per_row = transport.request_bytes / rows
    response_per_row = transport.response_bytes / rows
    print(
        f"[real-library] first sync: {rows} rows, {transport.request_bytes} "
        f"request bytes ({request_per_row:.0f}/row), "
        f"{transport.response_bytes} response bytes "
        f"({response_per_row:.0f}/row), {transport.requests} HTTP calls, "
        f"{elapsed:.2f}s wall"
    )
    assert request_per_row < MAX_FIRST_SYNC_BYTES_PER_ROW
    assert response_per_row < MAX_FIRST_SYNC_BYTES_PER_ROW
    assert elapsed < MAX_FIRST_SYNC_SECONDS


# ----- a real playlist ---------------------------------------------------------


def test_a_real_playlist_edited_on_one_spoke_converges(
    real_library_hub_and_spokes: RealLibraryFleet,
) -> None:
    """A real playlist reordered and trimmed on A arrives on B as that list."""
    fleet = real_library_hub_and_spokes
    _round_robin(fleet)

    conn = _open(fleet.spoke_a.data_dir)
    try:
        playlist_id, name, size = largest_playlist(conn)
        before = members_of(conn, playlist_id)
        assert size >= 3, f"{name!r} has {size} members; too small to reorder"
        # Reversed AND with the head dropped: a merge that appended instead of
        # replacing would still contain the dropped track.
        after = tuple(reversed(before[1:]))
        _replace_members(
            conn, playlist_id, after, sync_stamp.local_machine_id(conn)
        )
    finally:
        conn.close()
    print(
        f"[real-library] edited playlist {name!r}: {size} -> {len(after)} members"
    )

    _sync(fleet, fleet.spoke_a)
    _sync(fleet, fleet.spoke_b)

    conn = _open(fleet.spoke_b.data_dir)
    try:
        assert members_of(conn, playlist_id) == after
    finally:
        conn.close()

    _sync(fleet, fleet.spoke_a)
    _assert_converged(fleet)


# ----- a real tombstone --------------------------------------------------------


@pytest.mark.parametrize(
    "real_library_subset_tracks", [SMALL_SUBSET_TRACKS], indirect=True
)
def test_a_real_track_tombstoned_on_one_spoke_disappears_fleet_wide(
    real_library_hub_and_spokes: RealLibraryFleet,
) -> None:
    """A soft delete on a real track propagates and does not resurrect.

    Runs on a smaller subset (the ``indirect`` parametrize, which also proves
    the knob works) because the behavior is per-row: 120 real tracks exercise
    it exactly as 500 do.
    """
    fleet = real_library_hub_and_spokes
    assert fleet.subset_tracks == SMALL_SUBSET_TRACKS
    _round_robin(fleet)

    conn = _open(fleet.spoke_a.data_dir)
    try:
        victim, title = conn.execute(
            "SELECT stable_id, title FROM tracks ORDER BY stable_id LIMIT 1"
        ).fetchone()
        machine_id = sync_stamp.local_machine_id(conn)
        stamp = sync_stamp.stamp_and_log(conn, "tracks", (victim,), machine_id)
        conn.execute(
            "UPDATE tracks SET deleted_at = ?, updated_at = ?, "
            "origin_device_id = ? WHERE stable_id = ?",
            (stamp.updated_at, stamp.updated_at, machine_id, victim),
        )
    finally:
        conn.close()
    print(f"[real-library] tombstoned {title!r} ({victim})")

    _sync(fleet, fleet.spoke_a)
    _sync(fleet, fleet.spoke_b)

    for label, conn in _connections(fleet):
        row = conn.execute(
            "SELECT deleted_at FROM tracks WHERE stable_id = ?", (victim,)
        ).fetchone()
        assert row is not None, f"{label} hard-deleted the row instead"
        assert row[0] == stamp.updated_at, label
        assert (
            conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
            == fleet.subset_tracks
        ), label

    # A third sync must not resurrect it: a peer still holding a live copy
    # would push it back and the tombstone would lose to a row it outranks.
    _sync(fleet, fleet.spoke_a)
    _assert_converged(fleet)
    for label, conn in _connections(fleet):
        assert (
            conn.execute(
                "SELECT deleted_at FROM tracks WHERE stable_id = ?", (victim,)
            ).fetchone()[0]
            == stamp.updated_at
        ), label


# ----- relinking: the natural-key path at scale --------------------------------


def test_relinked_track_locations_converge_on_the_natural_key(
    real_library_hub_and_spokes: RealLibraryFleet,
) -> None:
    """Re-minting 250 real ``location_id`` values converges without duplicates.

    This is what a relink or a re-ingest does: the same file on the same
    machine, a freshly minted ``location_id``. Each incoming row therefore
    collides with a local row on ``(stable_id, machine_id, kind, file_path)``
    under a DIFFERENT primary key -- round 1 finding 1's exact shape, 250
    times over. Degrading here means duplicate natural keys, a growing table,
    or a sync that needs extra settling rounds to get there.
    """
    fleet = real_library_hub_and_spokes
    _round_robin(fleet)
    before = _fleet_digests(fleet)["hub"]
    conn = _open(fleet.hub_dir)
    try:
        locations_before = table_counts(conn)["track_locations"]
    finally:
        conn.close()

    conn = _open(fleet.spoke_a.data_dir)
    try:
        machine_id = sync_stamp.local_machine_id(conn)
        old_ids = [
            str(row[0])
            for row in conn.execute(
                "SELECT location_id FROM track_locations WHERE machine_id = ? "
                "ORDER BY location_id LIMIT ?",
                (machine_id, RELINKED_LOCATIONS),
            )
        ]
        assert len(old_ids) == RELINKED_LOCATIONS
        new_ids = _relink(conn, old_ids, machine_id)
    finally:
        conn.close()

    pushed = _sync(fleet, fleet.spoke_a)
    assert pushed.accepted == RELINKED_LOCATIONS
    assert pushed.rejected == 0
    assert pushed.rounds == 1, "the relink needed a settling round"
    pulled = _sync(fleet, fleet.spoke_b)
    assert pulled.applied == RELINKED_LOCATIONS
    _sync(fleet, fleet.spoke_a)

    digests = _assert_converged(fleet)
    assert (
        digests["hub"].tables["track_locations"]
        != before.tables["track_locations"]
    ), "the relink changed nothing; the assertions below would pass vacuously"
    placeholders = ", ".join("?" * len(old_ids))
    for label, conn in _connections(fleet):
        count = table_counts(conn)["track_locations"]
        assert count == locations_before, (
            f"{label} grew from {locations_before} to {count} track_locations "
            f"rows: the natural key did not resolve and both copies survived"
        )
        assert _duplicate_natural_keys(conn) == 0, label
        surviving = {
            str(row[0])
            for row in conn.execute(
                "SELECT location_id FROM track_locations WHERE location_id LIKE ?",
                (f"{RELINK_ID_PREFIX}%",),
            )
        }
        assert surviving == set(new_ids), label
        stale = int(
            conn.execute(
                f"SELECT COUNT(*) FROM track_locations WHERE location_id IN "
                f"({placeholders})",
                tuple(old_ids),
            ).fetchone()[0]
        )
        assert stale == 0, f"{label} still holds {stale} superseded rows"
    print(
        f"[real-library] relinked {RELINKED_LOCATIONS} of {locations_before} "
        f"track_locations rows; count unchanged, 0 duplicate natural keys"
    )


# ----- what the real library taught this tier ----------------------------------


def test_the_real_library_needed_the_documented_stamp_repair(
    real_library_db: PreparedLibrary,
) -> None:
    """The v5 library really does hold stamps the protocol cannot order.

    This tier's own premise, asserted rather than assumed. If a future library
    needs no repair, this fails and says so, instead of the fixture's repair
    step and the test below quietly stopping testing anything.
    """
    assert real_library_db.repairs, (
        "the real library needed no stamp repair; this tier no longer "
        "exercises normalize_stamps against the data it exists for"
    )
    print(
        f"[real-library] normalize_stamps repaired "
        f"{len(real_library_db.repairs)} stored stamps: "
        f"{real_library_db.repairs_by_reason}"
    )
    assert "naive-assumed-utc" in real_library_db.repairs_by_reason
