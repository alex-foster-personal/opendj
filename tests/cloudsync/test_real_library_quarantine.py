"""Round 5 quarantine, driven by the REAL library rather than a fixture stamp.

Companion to :mod:`tests.cloudsync.test_hub_sync_quarantine`, which proves the
same contract over rows a fixture invented. This module proves it over the
production 9,986-track library, which is where the defect was FOUND: it stores
20 timestamps carrying no UTC offset in ``tracks`` and 20 more in
``track_locations``, all of them SQLite's own ``CURRENT_TIMESTAMP`` spelling,
and round 4's readers raised on every one of them.

Split out of :mod:`tests.cloudsync.test_real_library_sync` for the
quality-gate file_size ratchet. The whole tier SKIPS where the library is
absent; see ``tests/cloudsync/conftest.py``.

Acceptance criteria, one test each:
- if an unorderable stored stamp does NOT quarantine exactly its own row and
  its FK dependants -- stopping the whole push, or reaching the peer with a
  fabricated stamp -- round 5's quarantine is gone in one of its two failure
  directions -- broken.
- if the repair pass does not put a quarantined row back in the sync set, the
  one command that unblocks a legacy library does not work -- broken.

Numbers this module measures are printed. Run with ``-s`` to see them.
"""
from __future__ import annotations

import pytest

from apps.shared.state import db as state_db
from apps.shared.state import normalize_stamps, sync_stamp
from apps.sync_hub import client, protocol, sync_set

from .real_library import PreparedLibrary, RealLibraryFleet, SeededSpoke
from .test_hub_sync import _open
from .test_real_library_sync import HUB_URL, SMALL_SUBSET_TRACKS

pytestmark = pytest.mark.requirement("CAT-04")


@pytest.fixture(autouse=True)
def _no_hub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``MDT_IS_HUB`` from the developer's shell must not steer these tests."""
    monkeypatch.delenv("MDT_IS_HUB", raising=False)


def _sync(fleet: RealLibraryFleet, spoke: SeededSpoke) -> client.SyncResult:
    """One full sync for ``spoke`` over the fleet's counting transport."""
    return client.run_sync(
        spoke.data_dir,
        HUB_URL,
        transport=fleet.transport,
        name=spoke.data_dir.name,
    )


@pytest.mark.parametrize(
    "real_library_subset_tracks", [SMALL_SUBSET_TRACKS], indirect=True
)
def test_one_unorderable_stored_stamp_quarantines_its_row_and_nothing_else(
    real_library_hub_and_spokes: RealLibraryFleet,
    real_library_db: PreparedLibrary,
) -> None:
    """T1 + T2 + T4. One real naive stamp costs ONE row, not the machine.

    Round 4 aborted here: ``protocol.canonical_row`` called
    ``canonical_timestamp`` -> ``to_canonical`` -> ``parse_canonical``, which
    RAISES, so one row of the shape SQLite's own ``CURRENT_TIMESTAMP`` writes
    stopped every push and every digest on that machine, forever. The
    superseded test asserted that raise and said in its own docstring that
    making it stop raising "is the FIX and not a regression"; this is that
    replacement.

    The value written below is a REAL one, lifted from the library's own
    repair list rather than invented.
    """
    fleet = real_library_hub_and_spokes
    legacy = real_library_db.first_repair("naive-assumed-utc")
    # Control: the value really is of the shape under test, not merely one
    # this pass happened to rewrite.
    with pytest.raises(sync_stamp.SyncStampError):
        sync_stamp.parse_canonical(legacy.stored)

    conn = _open(fleet.spoke_a.data_dir)
    try:
        victim = conn.execute(
            "SELECT stable_id FROM tracks ORDER BY stable_id LIMIT 1"
        ).fetchone()[0]
        clean_digest = protocol.sync_digest(conn)
        conn.execute(
            "UPDATE tracks SET updated_at = ? WHERE stable_id = ?",
            (legacy.stored, victim),
        )
        conn.commit()
        # T2: the digest ANSWERS, excluding the row and saying so.
        poisoned = protocol.sync_digest(conn)
    finally:
        conn.close()
    # The digest excludes the SAME set the offer holds back, transitively:
    # the track plus its dependants. It has to, or the two sides would
    # disagree forever about rows neither of them can exchange, and the ADR
    # 04 c6 corruption alarm would fire on ordinary legacy data.
    assert poisoned.quarantined is not None, "this digest must report, not abstain"
    assert poisoned.quarantined["tracks"] == 1
    assert set(poisoned.quarantined) == {
        "tracks", "track_fields", "track_locations", "track_vendor_ids",
    }
    assert poisoned.quarantined_rows is not None and poisoned.quarantined_rows > 1
    assert poisoned.tables["tracks"] != clean_digest.tables["tracks"], (
        "excluding a row must change that table's hash; equal hashes here "
        "would mean the digest had been made vacuously agreeable"
    )

    # T1: the sync completes, offers everything else, and counts what it
    # held back. The count is >1 because quarantine is TRANSITIVE over the FK
    # graph: this track's own track_fields / track_vendor_ids /
    # track_locations go with it, or the hub answers the whole chunk with a
    # FOREIGN KEY 409 no retry gets past. What must NOT happen is the whole
    # push stopping, which is what the assertion below pins.
    result = _sync(fleet, fleet.spoke_a)
    assert result.quarantined >= 1
    # The two exclusion SETS agree; the two TOTALS do not, and the difference
    # is exactly the membership rule. The digest counts every held
    # ``playlist_memberships`` row; the offer holds the whole PLAYLIST back
    # once and never counts membership rows at all, because membership never
    # travels on its own (ADR 04 c5). Asserting the raw totals equal passes
    # only where the poisoned track belongs to no playlist, which is a
    # property of this subset rather than of the engine -- on the full
    # library the same assertion reads 62 == 82.
    membership_rows = poisoned.quarantined.get(protocol.MEMBERSHIP_TABLE, 0)
    assert result.quarantined == poisoned.quarantined_rows - membership_rows, (
        "the digest and the offer must exclude the same rows, or two "
        "converged peers disagree forever"
    )
    assert result.pushed == fleet.spoke_a.pushable_rows - result.quarantined
    assert result.pushed > 0.9 * fleet.spoke_a.pushable_rows, (
        "one legacy stamp must cost one track and its dependants, not the "
        "library"
    )

    # T4: NOTHING was fabricated. Under the coalescing design the peer would
    # now hold this row stamped year zero; under quarantine it holds no row
    # at that pk at all, and the spoke still holds the original bytes.
    hub_conn = _open(fleet.hub_dir)
    try:
        assert hub_conn.execute(
            "SELECT COUNT(*) FROM tracks WHERE stable_id = ?", (victim,)
        ).fetchone()[0] == 0
    finally:
        hub_conn.close()
    conn = _open(fleet.spoke_a.data_dir)
    try:
        assert conn.execute(
            "SELECT updated_at FROM tracks WHERE stable_id = ?", (victim,)
        ).fetchone()[0] == legacy.stored, (
            "quarantine must not rewrite the row it holds back"
        )
    finally:
        conn.close()
    print(
        f"[real-library] one stored {legacy.stored!r} in tracks.updated_at "
        f"quarantines 1 row; the other {result.pushed} synced"
    )


def test_the_repair_puts_a_quarantined_row_back_in_the_sync_set(
    real_library_hub_and_spokes_unrepaired: RealLibraryFleet,
    real_library_db_unrepaired: PreparedLibrary,
) -> None:
    """T6, over the library exactly as it is on disk.

    The unrepaired fleet is the fixture round 5 added: every other fixture in
    this suite mints canonical stamps and ``real_library_db`` repairs before
    handing anything out, so nothing could put a real legacy stamp in a local
    table. Here the spokes carry the library's own.
    """
    fleet = real_library_hub_and_spokes_unrepaired
    # Control, stated as a presence: this fleet really does start dirty. An
    # equality assertion after the repair would otherwise be satisfied by two
    # empty sets.
    before = _sync(fleet, fleet.spoke_a)
    assert before.quarantined > 0, (
        "the unrepaired tier carried no quarantined row, so the repair "
        "assertion below would pass vacuously"
    )

    conn = state_db.open_rw(client.state_db_path(fleet.spoke_a.data_dir))
    try:
        repairs = normalize_stamps.scan(conn)
        assert repairs, "scan found nothing to repair on a dirty spoke"
        normalize_stamps.apply_repairs(conn, repairs)
    finally:
        conn.close()

    after = _sync(fleet, fleet.spoke_a)
    assert after.quarantined == 0
    assert after.pushed >= before.quarantined, (
        "the rows the repair freed must actually be offered"
    )
    hub_conn = _open(fleet.hub_dir)
    try:
        hub_tracks = int(
            hub_conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        )
    finally:
        hub_conn.close()
    assert hub_tracks == fleet.subset_tracks, (
        "after the repair every track must have reached the hub"
    )
    print(
        f"[real-library] repair freed {before.quarantined} quarantined row(s); "
        f"the hub now holds all {hub_tracks} tracks"
    )


def test_the_operator_count_and_the_digest_are_one_computation(
    real_library_db_unrepaired: PreparedLibrary,
    real_library_db: PreparedLibrary,
) -> None:
    """The number the panel shows must be the number the digest excluded.

    Measured over the library exactly as it is on disk, where the two used to
    disagree by more than half: a stamp-only count read 40 (stamp
    faults only) while ``sync_digest`` excluded 82 (stamp faults, the FK
    cascade, and the membership bundles those hold back). The panel's own
    hover text says "rows that are NOT in the sync set", so the smaller
    number was answering a different question than its label.

    The control is stated as a presence: the library really does exclude
    rows, so an equality between two zeros cannot satisfy this.
    """
    conn = state_db.open_rw(real_library_db_unrepaired.path)
    try:
        digest = protocol.sync_digest(conn)
        counted = sum(sync_set.excluded_counts(conn).values())
        per_table = sync_set.excluded_counts(conn)
    finally:
        conn.close()

    assert digest.quarantined_rows, "the unrepaired library excluded nothing"
    assert counted == digest.quarantined_rows
    assert per_table == digest.quarantined
    # The cascade really is what makes the two numbers differ from a naive
    # stamp count: more tables are affected than the two that hold the
    # library's own naive stamps.
    assert set(per_table) > {"tracks", "track_locations"}

    # The other arm, and the one that makes the fast path safe: on the
    # REPAIRED library `excluded_counts` skips the transitive walk entirely
    # (nothing can root an exclusion), so it must agree with a digest that
    # did walk every row. A short circuit that answered "none" for the wrong
    # reason would pass the dirty arm above and fail here.
    clean = state_db.open_rw(real_library_db.path)
    try:
        assert sync_set.any_stamp_fault(clean) is False
        assert sync_set.excluded_counts(clean) == {}
        assert protocol.sync_digest(clean).quarantined == {}
        assert sum(sync_set.excluded_counts(clean).values()) == 0
    finally:
        clean.close()
    print(f"[real-library] rows outside the sync set: {counted} {per_table}")
