"""Migration 22 -> 23: ``tracks.restored_at`` and ``tracks.deleted_reason`` (LIBM-140).

``tracks.deleted_at`` records that the user removed a track. Nothing recorded
that the user deliberately brought one back, so a merge could not tell a
restore from any other write that happened to leave ``deleted_at`` NULL: a
vendor re-ingest on a machine that never saw the tombstone, or a stale copy of
the library. Both beat the tombstone on stamp order and resurrected the track
fleet-wide (issue #4628).

``restored_at`` is the stamp of the last explicit restore
(``StateWriter.undelete_track``). It stays set after a later delete, so the
pair ``(deleted_at, restored_at)`` orders a row's lifecycle on every machine:
the later of the two decides, and only then does ``updated_at`` break a tie
(``apps.sync_hub.engine_apply``). NULL means "never restored".

``deleted_reason`` says WHY a tombstone exists, because ``deleted_at`` has two
writers that must not be confused: ``'user'`` for Remove from library, and
``'missing'`` for a watched-folder rescan that found the file gone. Only a
``'missing'`` tombstone may be lifted by an ingest that finds the file again.
NULL on a tombstone is one written before this step; it is read as ``'user'``,
so an old delete stays deleted and is restored with ``undelete`` if unwanted.

Synced columns, so this step is also sync wire v7
(``apps.sync_hub.wire_version``). Rollout order: hub first.
"""

from __future__ import annotations

_V23: list[str] = [
    "ALTER TABLE tracks ADD COLUMN restored_at TEXT",
    "ALTER TABLE tracks ADD COLUMN deleted_reason TEXT",
]

__all__ = ["_V23"]
