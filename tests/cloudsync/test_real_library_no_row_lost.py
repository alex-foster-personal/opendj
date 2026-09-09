"""The data-loss lens: over the real library, no row is ever lost.

The round-1 review named this as the verification that had never run, for a
change whose entire purpose is not losing rows. Its companions prove the
quarantine RULE (:mod:`tests.cloudsync.test_real_library_quarantine`, one
poisoned row costs one row) and the mixed-version CONTRACT
(:mod:`tests.cloudsync.test_hub_sync_mixed_version`, an old spoke is refused
rather than served a short pull). Neither of them asks the blunt question
this module asks: after the fleet has done everything it is going to do, is
EVERY row still there.

Three properties, and each is stated as a PRESENCE rather than as the absence
of a failure.

1. **The fence cannot livelock.** A spoke holding rows it cannot order syncs
   repeatedly forever, because that is what a background sync does. Round 5
   stops the fence BELOW the held row so the row is re-offered after a
   repair. That is exactly the shape that spins: a fence that refuses to
   advance re-offers the same window every pass. So the second and later
   syncs must push NOTHING NEW, hold the SAME rows, and leave the fence where
   it was -- settled, not stuck re-sending.

2. **The repair converges without a generation rotate.**
   ``generation.rotate`` is the hub's restore token: it makes every spoke
   re-offer its whole library. If convergence needed it, "no row lost" would
   be true only after an operator ran a recovery command, which is not the
   claim. So the fleet is asserted to reach parity with the rotate NEVER
   called, and the assertion is guarded by a control that the rotate really
   would have been observable had it happened.

3. **Every table, not just ``tracks``.** The existing repair test counts
   tracks on the hub. A row can be lost in ``track_fields`` or
   ``playlist_memberships`` while every track is present, so parity is
   compared table by table across the WHOLE sync set, and the comparison is
   two-sided: the hub must hold no FEWER rows than the spoke and no MORE.

Regression lines:
  - if a second sync on a dirty library pushes rows the first already pushed
    then the fence is re-sending and broken
  - if the fence regresses between two syncs then a settled position is not
    settled and broken
  - if any sync-set table's hub count differs from the spoke's after the
    repair then a row was lost or invented, so broken
  - if the parity assertion passes while the fleet never held anything back
    then it passed vacuously and is broken
  - if convergence needed generation.rotate then "no row lost" is untrue
    without an operator, so broken

Numbers this module measures are printed. Run with ``-s`` to see them.
"""
from __future__ import annotations

import sqlite3

import pytest

from apps.shared.state import db as state_db
from apps.shared.state import normalize_stamps
from apps.sync_hub import client, engine_watermark, generation, protocol

from .real_library import PreparedLibrary, RealLibraryFleet, SeededSpoke
from .test_hub_sync import _open
from .test_real_library_sync import HUB_URL

pytestmark = pytest.mark.requirement("CAT-04")

#: How many extra syncs to run once the fleet has nothing left to say. Four
#: rather than two: a livelock with a period of two passes would survive a
#: single repeat, and this is cheap.
SETTLED_PASSES: int = 4

#: Both tests run on the fixture's DEFAULT subset (500 tracks), not the small
#: one. The 120-track slice carries none of the library's 60 naive stamps, so
#: it holds nothing back and both controls below refuse it -- which is the
#: fixture behaving correctly, not a size to work around.


@pytest.fixture(autouse=True)
def _no_hub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``MDT_IS_HUB`` from the developer's shell must not steer these tests."""
    monkeypatch.delenv("MDT_IS_HUB", raising=False)


# ----- helpers ---------------------------------------------------------------


def _sync(fleet: RealLibraryFleet, spoke: SeededSpoke) -> client.SyncResult:
    return client.run_sync(
        spoke.data_dir, HUB_URL, transport=fleet.transport, name=spoke.data_dir.name
    )


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Live row count for every table the sync set carries.

    Tombstones included on purpose: a soft-deleted row is still a row that
    must reach the peer (ADR 04 c4), and filtering them here would let a
    dropped tombstone read as parity.
    """
    return {
        spec.name: int(
            conn.execute(f"SELECT COUNT(*) FROM {spec.name}").fetchone()[0]
        )
        for spec in protocol.SYNC_TABLES
    }


def _fence(spoke: SeededSpoke) -> int:
    """This spoke's push fence against the hub, read from its own state DB."""
    conn = state_db.open_rw(client.state_db_path(spoke.data_dir))
    try:
        return int(engine_watermark.read_watermark(conn, "hub").last_push_seq)
    finally:
        conn.close()


def _repair(spoke: SeededSpoke) -> int:
    """Run the documented operator pass on ``spoke``; return what it fixed."""
    conn = state_db.open_rw(client.state_db_path(spoke.data_dir))
    try:
        repairs = normalize_stamps.scan(conn)
        assert repairs, "scan found nothing to repair on a dirty spoke"
        normalize_stamps.apply_repairs(conn, repairs)
        return len(repairs)
    finally:
        conn.close()


# ----- the lens --------------------------------------------------------------


def test_a_dirty_library_settles_instead_of_re_offering_forever(
    real_library_hub_and_spokes_unrepaired: RealLibraryFleet,
) -> None:
    """Property 1. The held-back fence must SETTLE, not spin.

    Round 5 stops the fence below the held row so a repair re-offers it. That
    is the same shape as a fence that never advances, and the difference
    between "settled" and "stuck re-sending" is invisible from one sync.
    """
    fleet = real_library_hub_and_spokes_unrepaired
    spoke = fleet.spoke_a

    first = _sync(fleet, spoke)
    # Control, as a presence: this fleet really is holding rows back. Without
    # it every assertion below is satisfied by a fleet with nothing to hold.
    assert first.quarantined > 0, (
        "the unrepaired tier held nothing back, so the livelock assertions "
        "below would pass vacuously"
    )

    fences = [_fence(spoke)]
    pushed: list[int] = []
    quarantined: list[int] = []
    for _ in range(SETTLED_PASSES):
        result = _sync(fleet, spoke)
        pushed.append(result.pushed)
        quarantined.append(result.quarantined)
        fences.append(_fence(spoke))

    assert pushed == [0] * SETTLED_PASSES, (
        f"a settled spoke re-sent rows on a later pass: pushed={pushed}. The "
        f"fence is re-offering its window, which is a livelock however many "
        f"times it succeeds"
    )
    assert quarantined == [first.quarantined] * SETTLED_PASSES, (
        f"the held-back set moved while nothing was written: "
        f"first={first.quarantined}, later={quarantined}"
    )
    assert fences == sorted(fences), f"the push fence regressed: {fences}"
    assert len(set(fences)) == 1, (
        f"the fence kept moving on a quiet library: {fences}. A fence that "
        f"advances with nothing to advance past is stepping over held rows"
    )
    # What a CONSTANT fence does not by itself prove is that the fence can
    # still ADVANCE while rows are held -- a fence pinned forever would also
    # read constant here. That direction is
    # test_hub_sync_quarantine_fence.py::
    # test_an_ordinary_write_after_a_quarantined_sync_still_syncs, which
    # writes past a quarantined sync and watches the row travel. This test
    # owns the other direction: quiet means quiet.
    print(
        f"[data-loss lens] dirty library: {first.quarantined} held, fence "
        f"parked at {fences[0]} across {SETTLED_PASSES} further syncs, "
        f"0 rows re-sent"
    )


def test_no_row_is_lost_once_the_repair_runs_and_no_rotate_is_needed(
    real_library_hub_and_spokes_unrepaired: RealLibraryFleet,
    real_library_db_unrepaired: PreparedLibrary,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Properties 2 and 3. Parity across the WHOLE sync set, no rotate.

    The blunt question: after the fleet has done everything it is going to
    do, is every row still there. Table by table, two-sided, tombstones
    included.
    """
    fleet = real_library_hub_and_spokes_unrepaired
    spoke = fleet.spoke_a

    rotates: list[str] = []

    def _forbidden(*args: object, **kwargs: object) -> str:
        rotates.append("called")
        raise AssertionError(
            "generation.rotate was called; convergence must not need the "
            "hub's restore token"
        )

    # Control on the guard itself: patching a name that no longer exists
    # would make this test pass by watching nothing, so raising=True.
    monkeypatch.setattr(generation, "rotate", _forbidden, raising=True)

    before = _sync(fleet, spoke)
    assert before.quarantined > 0, (
        "the unrepaired tier held nothing back, so the parity assertion "
        "below would pass over a fleet that never had a row at risk"
    )

    spoke_conn = _open(spoke.data_dir)
    try:
        spoke_counts = _counts(spoke_conn)
    finally:
        spoke_conn.close()

    hub_conn = _open(fleet.hub_dir)
    try:
        held_back = _counts(hub_conn)
    finally:
        hub_conn.close()
    # Control that the parity below is not trivially true from the start:
    # SOME table must be short while rows are held.
    assert any(
        held_back[table] < spoke_counts[table] for table in spoke_counts
    ), (
        f"the hub already matched the spoke before the repair, so the "
        f"post-repair equality proves nothing: {held_back} vs {spoke_counts}"
    )

    repaired = _repair(spoke)
    after = _sync(fleet, spoke)
    assert after.quarantined == 0, (
        f"{after.quarantined} row(s) still held after the documented repair"
    )

    hub_conn = _open(fleet.hub_dir)
    try:
        hub_counts = _counts(hub_conn)
    finally:
        hub_conn.close()

    short = {
        table: (hub_counts[table], spoke_counts[table])
        for table in spoke_counts
        if hub_counts[table] < spoke_counts[table]
    }
    assert not short, f"the hub LOST rows (hub, spoke): {short}"
    extra = {
        table: (hub_counts[table], spoke_counts[table])
        for table in spoke_counts
        # track_locations is a per-machine fact (ADR 08 point 1): the hub
        # accumulates one set per spoke, so more is correct there and only
        # there.
        if table != "track_locations" and hub_counts[table] > spoke_counts[table]
    }
    assert not extra, f"the hub INVENTED rows (hub, spoke): {extra}"
    assert not rotates
    print(
        f"[data-loss lens] {before.quarantined} held -> {repaired} stamps "
        f"repaired -> 0 held; every sync-set table at parity: {hub_counts}"
    )
