"""Merge rule for a ``tracks`` row's lifecycle: a tombstone is never dropped.

Contract: ``docs/decisions/ADR-NEW-deletes-stay-deleted.md`` (CLOUDSYNC-29,
issue #4628, wire v7).

Requirements (status: done + ran + regression tests):

1. A tombstone outranks every write that is not a later explicit restore.
   - [if] the stored row is removed and the incoming row is live with a newer
     ``updated_at`` but no later ``restored_at`` [then] the incoming row loses.
   - [if] the incoming row is removed and the stored row is live, never
     restored since [then] the incoming tombstone wins although its stamp is
     older.
   - [if] the incoming row is live with ``restored_at`` later than the stored
     ``deleted_at`` [then] the restore wins.
2. Rows in the same lifecycle state still merge by ``updated_at``.
   - [if] both rows carry the same lifecycle key [then] this module has no
     verdict and last-writer-wins decides.
3. A stored lifecycle stamp that cannot be ordered quarantines the incoming
   row; nothing is compared on a guess.
4. A track row is hard-deleted only when an identity remap names it as the
   loser, so its recording lives on under the survivor's id.
   - [if] no remap names the row [then] the delete is refused.

Plain row-level LWW could not hold a delete: a vendor re-ingest on a machine
that had not pulled the tombstone, or a stale copy of the library, stamps the
row later than the delete, and the later stamp won on every machine.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from apps.shared.state import sync_stamp
from apps.sync_hub import protocol
from apps.sync_hub.engine_common import SyncApplyError
from apps.sync_hub.engine_identity_map import load_identity_remap
from apps.sync_hub.protocol import RowChange

TRACKS_TABLE: str = protocol.TRACKS_TABLE
_LIFECYCLE_COLUMNS: tuple[str, str] = (protocol.DELETED_AT, protocol.RESTORED_AT)

# -----------------------------------------------------------------------------
# types
# -----------------------------------------------------------------------------


class TrackHardDeleteRefusedError(SyncApplyError):
    """A ``tracks`` row was about to be hard-deleted with nothing left behind."""


@dataclass(frozen=True)
class LifecycleVerdict:
    """What the lifecycle keys decided about one incoming ``tracks`` row.

    ``tombstone_held`` marks the one case a caller must act on beyond
    rejecting the row: a LIVE incoming row lost to a stored tombstone, so its
    sender still holds a resurrected copy and has to be told about the
    tombstone again.
    """

    incoming_loses: bool
    tombstone_held: bool = False
    faults: tuple[protocol.StampFault, ...] = ()


# -----------------------------------------------------------------------------
# merge rule
# -----------------------------------------------------------------------------


def judge(conn: sqlite3.Connection, change: RowChange) -> LifecycleVerdict | None:
    """Decide ``change`` on lifecycle alone, or None when LWW must decide.

    None for any table but ``tracks``, for a row not stored yet, and for two
    rows in the same lifecycle state.
    """
    if change.table != TRACKS_TABLE:
        return None
    stored = conn.execute(
        f"SELECT {', '.join(_LIFECYCLE_COLUMNS)} FROM {TRACKS_TABLE} WHERE stable_id = ?",
        (change.pk[0],),
    ).fetchone()
    if stored is None:
        return None
    stored_values = dict(zip(_LIFECYCLE_COLUMNS, stored, strict=True))
    faults = tuple(
        protocol.StampFault(table=TRACKS_TABLE, column=column, value=value)
        for column, value in stored_values.items()
        if value is not None and not sync_stamp.is_orderable(str(value))
    )
    if faults:
        return LifecycleVerdict(incoming_loses=False, faults=faults)
    stored_key = protocol.lifecycle_key(stored_values)
    incoming_key = protocol.lifecycle_key(change.values)
    if incoming_key == stored_key:
        return None
    incoming_loses = incoming_key < stored_key
    return LifecycleVerdict(
        incoming_loses=incoming_loses,
        tombstone_held=(
            incoming_loses
            and stored_values[protocol.DELETED_AT] is not None
            and change.values.get(protocol.DELETED_AT) is None
        ),
    )


# -----------------------------------------------------------------------------
# hard-delete guard
# -----------------------------------------------------------------------------


def assert_hard_delete_allowed(conn: sqlite3.Connection, stable_id: str) -> None:
    """Refuse to hard-delete a track that no identity remap accounts for.

    A hard delete leaves no tombstone, so any machine still holding the row
    re-inserts it on its next sync and nothing objects (issue #4628: 166
    tracks dropped this way had no tombstone on the hub). The one sanctioned
    hard delete is an identity collapse, where the row's recording survives
    under another id and ``sync_identity_remap`` records which. Everything
    else removes a track with ``StateWriter.remove_from_library``.
    """
    if stable_id not in load_identity_remap(conn):
        raise TrackHardDeleteRefusedError(
            f"refusing to hard-delete tracks row {stable_id}: no identity remap "
            "names it as a collapsed duplicate, so the delete would leave no "
            "tombstone and any machine still holding the row would resurrect "
            "it. Remove a track with StateWriter.remove_from_library "
            "(POST /api/v1/tracks/{id}:remove), which tombstones it."
        )


__all__ = [
    "LifecycleVerdict",
    "TrackHardDeleteRefusedError",
    "assert_hard_delete_allowed",
    "judge",
]
