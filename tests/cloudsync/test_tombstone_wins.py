"""CLOUDSYNC-29: a track tombstone outranks every edit that is not a restore.

Contract: ``docs/decisions/ADR-NEW-deletes-stay-deleted.md`` (issue #4628).
Every scenario runs the real hub router and the real ``run_sync``; a sync that
leaves the two sides different raises ``SyncDigestMismatch``, so a test that
returns has also proven convergence.

- [if] a stale copy holding a removed track live syncs [then] it stays removed on every machine, [else stop].
- [if] a track is edited after another machine removed it [then] the older tombstone still wins, [else stop].
- [if] the user restores a removed track [then] the restore reaches every machine, [else stop].
- [if] a machine brings tracks the fleet has never held [then] they arrive, [else stop].

The stale copy's own row becomes removed too. The third and fourth lines are
the controls: the rule must not make a track un-restorable or refuse new ones.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter, compute_playlist_id
from apps.sync_hub import engine_apply, engine_identity_map
from apps.sync_hub.protocol import SPEC_BY_TABLE
from tests.cloudsync.test_hub_sync import (
    _DEV_A,
    _DEV_B,
    _T0,
    _insert_track,
    _log,
    _open,
    _seed_common_track,
    _set_track_title,
    _sync,
    _TestClientTransport,
)

pytestmark = pytest.mark.requirement("CLOUDSYNC-29")

_REMOVED_AT = datetime(2026, 8, 30, 10, 0, 0, tzinfo=UTC)
_RESTORED_AT = datetime(2026, 8, 30, 11, 0, 0, tzinfo=UTC)
_REMOVED_AGAIN_AT = datetime(2026, 8, 30, 12, 0, 0, tzinfo=UTC)
_STALE_EDIT = "2026-08-30T13:00:00.000000+00:00"
_STALE_RESTORE = "2026-08-30T11:00:00.000000+00:00"


@pytest.fixture
def spoke_c(tmp_path: Path) -> Path:
    """A third machine's empty data dir: the stale copy."""
    return tmp_path / "spoke-c"


# ----- helpers -----------------------------------------------------------


def _fixed_clock(moment: datetime) -> Callable[[], datetime]:
    ticks = {"n": 0}

    def clock() -> datetime:
        ticks["n"] += 1
        return moment.replace(microsecond=ticks["n"])

    return clock


def _lifecycle(data_dir: Path, action: str, stable_id: str, moment: datetime) -> None:
    """Remove or restore through the real writer, at a pinned instant."""
    conn = _open(data_dir)
    try:
        writer = StateWriter(conn, bus=FakeEventBus(), clock=_fixed_clock(moment), actor="test")
        try:
            if action == "remove":
                writer.remove_from_library(stable_id)
            elif action == "restore":
                writer.undelete_track(stable_id)
            else:
                raise AssertionError(f"unknown lifecycle action {action!r}")
        finally:
            writer.close()
    finally:
        conn.close()


def _state(data_dir: Path, stable_id: str) -> tuple[str | None, str | None] | None:
    """``(title, deleted_at)`` of one track, or None when the row is absent."""
    conn = _open(data_dir)
    try:
        row = conn.execute("SELECT title, deleted_at FROM tracks WHERE stable_id = ?", (stable_id,)).fetchone()
    finally:
        conn.close()
    return None if row is None else (row[0], row[1])


def _is_removed(data_dir: Path, stable_id: str) -> bool:
    state = _state(data_dir, stable_id)
    assert state is not None, f"{data_dir.name} holds no row for {stable_id}"
    return state[1] is not None


def _live_members(data_dir: Path, stable_id: str) -> int:
    conn = _open(data_dir)
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM playlist_memberships WHERE stable_id = ? AND deleted_at IS NULL",
                (stable_id,),
            ).fetchone()[0]
        )
    finally:
        conn.close()


def _add_to_a_playlist(data_dir: Path, stable_id: str) -> None:
    conn = _open(data_dir)
    try:
        writer = StateWriter(
            conn,
            bus=FakeEventBus(),
            clock=_fixed_clock(_REMOVED_AT.replace(hour=9)),
            actor="test",
        )
        try:
            playlist_id = compute_playlist_id("open-dj", "set-1")
            writer.insert_playlist(playlist_id=playlist_id, name="Set", vendor="open-dj", vendor_pl_id="set-1")
            writer.set_playlist_memberships(playlist_id, [stable_id])
        finally:
            writer.close()
    finally:
        conn.close()


def _edit(data_dir: Path, stable_id: str, title: str, updated_at: str) -> None:
    conn = _open(data_dir)
    try:
        _set_track_title(conn, stable_id, title=title, updated_at=updated_at, origin=_DEV_B)
    finally:
        conn.close()


def _removed_everywhere(hub: _TestClientTransport, spoke_a: Path, spoke_b: Path) -> None:
    """A and B hold ``trk-1``; A removes it; both have synced the tombstone."""
    _seed_common_track((spoke_a, spoke_b), "trk-1")
    _sync(spoke_a, hub, "spoke-a")
    _sync(spoke_b, hub, "spoke-b")
    _lifecycle(spoke_a, "remove", "trk-1", _REMOVED_AT)
    _sync(spoke_a, hub, "spoke-a")
    _sync(spoke_b, hub, "spoke-b")
    assert _is_removed(spoke_b, "trk-1")


# ----- the stale copy ------------------------------------------------------


def test_stale_copy_first_sync_does_not_resurrect_a_removed_track(
    hub: _TestClientTransport,
    hub_dir: Path,
    spoke_a: Path,
    spoke_b: Path,
    spoke_c: Path,
) -> None:
    _seed_common_track((spoke_a, spoke_b), "trk-1")
    _add_to_a_playlist(spoke_a, "trk-1")
    _sync(spoke_a, hub, "spoke-a")
    _sync(spoke_b, hub, "spoke-b")
    assert _live_members(spoke_b, "trk-1") == 1
    _lifecycle(spoke_a, "remove", "trk-1", _REMOVED_AT)
    _sync(spoke_a, hub, "spoke-a")
    _sync(spoke_b, hub, "spoke-b")

    # The stale copy: the same track, live, edited AFTER the fleet removed it,
    # plus one track the fleet has never held.
    conn_c = _open(spoke_c)
    try:
        _insert_track(conn_c, "trk-1", title="vendor edit", updated_at=_STALE_EDIT, origin=_DEV_B)
        _insert_track(conn_c, "trk-new", title="brand new", updated_at=_T0, origin=_DEV_B)
    finally:
        conn_c.close()

    _sync(spoke_c, hub, "spoke-c")
    _sync(spoke_b, hub, "spoke-b")
    _sync(spoke_a, hub, "spoke-a")

    for machine in (hub_dir, spoke_a, spoke_b, spoke_c):
        assert _is_removed(machine, "trk-1"), f"{machine.name}: a stale copy resurrected a removed track"
        assert _live_members(machine, "trk-1") == 0, f"{machine.name}: a removed track is back in a playlist"
        # Control: the first sync still delivers what is genuinely new.
        assert _state(machine, "trk-new") == ("brand new", None), (
            f"{machine.name}: a track new to the fleet did not arrive"
        )
    assert _state(hub_dir, "trk-1") == _state(spoke_a, "trk-1")


def test_a_new_machine_with_only_new_tracks_completes_its_first_sync(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path, spoke_b: Path, spoke_c: Path
) -> None:
    """Control for over-correction: a first sync is not refused wholesale."""
    _removed_everywhere(hub, spoke_a, spoke_b)
    conn_c = _open(spoke_c)
    try:
        for stable_id in ("new-1", "new-2"):
            _insert_track(conn_c, stable_id, title=stable_id, updated_at=_T0, origin=_DEV_B)
    finally:
        conn_c.close()

    result = _sync(spoke_c, hub, "spoke-c")

    assert result.accepted == 2
    assert _state(hub_dir, "new-1") == ("new-1", None)
    assert _state(hub_dir, "new-2") == ("new-2", None)
    assert _is_removed(spoke_c, "trk-1"), "the new machine did not learn the tombstone"


# ----- an edit newer than the tombstone ------------------------------------


def test_a_tombstone_older_than_a_later_edit_still_wins(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path, spoke_b: Path
) -> None:
    _seed_common_track((spoke_a, spoke_b), "trk-1")
    _sync(spoke_a, hub, "spoke-a")
    _sync(spoke_b, hub, "spoke-b")
    _lifecycle(spoke_a, "remove", "trk-1", _REMOVED_AT)
    _sync(spoke_a, hub, "spoke-a")
    # B has not pulled the tombstone yet, and edits the track after it.
    _edit(spoke_b, "trk-1", "edited after the delete", _STALE_EDIT)

    _sync(spoke_b, hub, "spoke-b")

    assert _is_removed(hub_dir, "trk-1"), "a newer edit beat the tombstone on the hub"
    assert _is_removed(spoke_b, "trk-1"), "the editing machine kept its live copy"


def test_a_resurrection_after_the_tombstone_was_pulled_is_healed(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path, spoke_b: Path
) -> None:
    """A machine that already pulled the tombstone and then brought the row
    back without a restore (what a pre-fix vendor re-ingest did) is refused by
    the hub AND handed the tombstone again, so it does not stay diverged."""
    _removed_everywhere(hub, spoke_a, spoke_b)
    conn_b = _open(spoke_b)
    try:
        stamped = _log(conn_b, "tracks", ("trk-1",), _DEV_B, _STALE_EDIT)
        conn_b.execute(
            "UPDATE tracks SET deleted_at = NULL, updated_at = ?, origin_device_id = ? WHERE stable_id = 'trk-1'",
            (stamped, _DEV_B),
        )
    finally:
        conn_b.close()

    _sync(spoke_b, hub, "spoke-b")

    assert _is_removed(hub_dir, "trk-1")
    assert _is_removed(spoke_b, "trk-1")


# ----- an explicit restore ---------------------------------------------------


def test_an_explicit_restore_reaches_every_machine(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path, spoke_b: Path
) -> None:
    """Overshoot control: tombstone-wins must not make a delete permanent."""
    _removed_everywhere(hub, spoke_a, spoke_b)

    _lifecycle(spoke_a, "restore", "trk-1", _RESTORED_AT)
    _sync(spoke_a, hub, "spoke-a")
    _sync(spoke_b, hub, "spoke-b")

    for machine in (hub_dir, spoke_a, spoke_b):
        assert not _is_removed(machine, "trk-1"), f"{machine.name}: an explicit restore did not win over the tombstone"


def test_a_restore_older_than_a_later_delete_loses(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path, spoke_b: Path, spoke_c: Path
) -> None:
    _removed_everywhere(hub, spoke_a, spoke_b)
    _lifecycle(spoke_a, "restore", "trk-1", _RESTORED_AT)
    _lifecycle(spoke_a, "remove", "trk-1", _REMOVED_AGAIN_AT)
    _sync(spoke_a, hub, "spoke-a")
    # The stale copy holds the row as it was BETWEEN the restore and the
    # second delete, then edited: live, restored at 11:00, stamped 13:00.
    conn_c = _open(spoke_c)
    try:
        _insert_track(conn_c, "trk-1", title="stale", updated_at=_STALE_EDIT, origin=_DEV_B)
        conn_c.execute(
            "UPDATE tracks SET restored_at = ? WHERE stable_id = 'trk-1'",
            (_STALE_RESTORE,),
        )
    finally:
        conn_c.close()

    _sync(spoke_c, hub, "spoke-c")

    assert _is_removed(hub_dir, "trk-1")
    assert _is_removed(spoke_c, "trk-1")


# ----- hard deletes -----------------------------------------------------------


def test_hard_deleting_a_track_is_refused_without_an_identity_remap(
    spoke_a: Path,
) -> None:
    spec = SPEC_BY_TABLE["tracks"]
    conn = _open(spoke_a)
    try:
        _insert_track(conn, "trk-1", title="keep", updated_at=_T0, origin=_DEV_A)
        _insert_track(conn, "trk-2", title="survivor", updated_at=_T0, origin=_DEV_A)

        with pytest.raises(engine_apply.TrackHardDeleteRefusedError):
            engine_apply._drop_superseded(conn, spec, ("trk-1",))
        assert _count(conn, "trk-1") == 1, "the refused delete still removed the row"

        # Control: an identity collapse, whose loser lives on as the survivor.
        remap: dict[str, str] = {}
        engine_identity_map.record_identity_remap(conn, remap, "trk-1", "trk-2")
        engine_apply._drop_superseded(conn, spec, ("trk-1",))
        assert _count(conn, "trk-1") == 0
    finally:
        conn.close()


def _count(conn: sqlite3.Connection, stable_id: str) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM tracks WHERE stable_id = ?", (stable_id,)).fetchone()[0])
