"""A pure last-writer-wins oracle for the CloudSync property simulation (plan W18).

The oracle knows nothing about sqlite, the hub, fences or the wire. It is
told every write the simulation makes, as ``(row, Version)``, and answers one
question: which version of each row SHOULD every machine hold once the fleet
has settled. That answer is the ADR 04 c3 rule: the version with the greatest
``(updated_at, origin_device_id)`` wins. For a playlist a tombstone is just a
version whose ``deleted_at`` is set. For a track, wire v7 (CLOUDSYNC-29) puts
one comparison in front of that: the version whose last removed-or-restored
event is latest wins, so a tombstone beats every write that is not a later
restore, and LWW orders only versions that agree on that event.

Membership semantics are ADR 04 c5 whole-playlist granularity: the member
list is part of the playlist row's content, so the playlist version that
wins LWW carries its whole list with it. That is the documented design, not
a guess; findings 5/5b (round 4) question whether it is the RIGHT design,
and an owner change there changes this module's ``content`` for playlists.

:func:`read_versions` is the only sqlite in here: it reads one machine's rows
into the same ``Version`` shape so the simulation can compare what a machine
holds against what the oracle says, row by row.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from apps.shared.state import sync_stamp
from apps.sync_hub import protocol, protocol_common

#: ``(table, primary key)``: one logical row across the whole fleet.
RowKey = tuple[str, str]
#: ``(canonical updated_at, origin_device_id)``: the pair ADR 04 c3 compares.
LwwKey = tuple[str, str]

TRACKS: str = "tracks"
PLAYLISTS: str = "playlists"


@dataclass(frozen=True)
class Version:
    """One write of one row: its LWW key and the domain content it carried.

    ``content`` for a track is ``(title, deleted_at, restored_at)``; for a playlist it is
    ``(name, deleted_at, member stable_ids in position order)``. Stamps are
    canonical so two spellings of one instant compare equal, as they do on
    the wire.
    """

    key: LwwKey
    content: tuple[Any, ...]


def canonical_or_none(value: str | None) -> str | None:
    return None if value is None else sync_stamp.to_canonical(str(value))


def lww_key(updated_at: str, origin: str) -> LwwKey:
    return protocol.lww_key({"updated_at": updated_at, "origin_device_id": origin})


def track_lifecycle_key(version: Version) -> str:
    """When a track version last moved between removed and live."""
    _title, deleted_at, restored_at = version.content
    return protocol_common.lifecycle_key({"deleted_at": deleted_at, "restored_at": restored_at})


class LwwOracle:
    """Every write ever made, per row. Pure Python; the reference model."""

    def __init__(self) -> None:
        self._versions: dict[RowKey, list[Version]] = defaultdict(list)

    def record(self, row: RowKey, version: Version) -> None:
        """Remember one write. A cross-origin tie on one row is refused, loudly.

        Two origins at one stamp fall to the tie-break on machine ids, which
        are random per run, so the winner would not replay and hypothesis
        would report a flaky strategy instead of a finding. The fleet's
        per-spoke clock offsets make such a tie impossible; this is the check.
        """
        stamp, origin = version.key
        tied = [known.key for known in self._versions[row] if known.key[0] == stamp]
        if any(known_origin != origin for _stamp, known_origin in tied):
            raise ValueError(f"{row}: {version.key} ties {tied} on stamp across origins")
        self._versions[row].append(version)

    def rows(self) -> tuple[RowKey, ...]:
        return tuple(sorted(self._versions))

    def winner(self, row: RowKey) -> Version:
        """The version LWW says every settled machine must hold."""
        versions = self._versions.get(row)
        if not versions:
            raise KeyError(f"the oracle was never told about {row}")
        if row[0] == TRACKS:
            return max(versions, key=lambda version: (track_lifecycle_key(version), version.key))
        return max(versions, key=lambda version: version.key)

    def is_known(self, row: RowKey, version: Version) -> bool:
        """True when ``version`` is exactly a write the simulation made."""
        return version in self._versions.get(row, ())


def read_versions(conn: sqlite3.Connection) -> dict[RowKey, Version]:
    """Every track and playlist row this machine holds, tombstones included."""
    versions: dict[RowKey, Version] = {}
    for stable_id, title, deleted_at, restored_at, updated_at, origin in conn.execute(
        "SELECT stable_id, title, deleted_at, restored_at, updated_at, origin_device_id FROM tracks"
    ):
        versions[(TRACKS, stable_id)] = Version(
            key=lww_key(updated_at, origin),
            content=(title, canonical_or_none(deleted_at), canonical_or_none(restored_at)),
        )
    members: dict[str, list[str]] = defaultdict(list)
    for playlist_id, stable_id in conn.execute(
        "SELECT playlist_id, stable_id FROM playlist_memberships ORDER BY playlist_id, position"
    ):
        members[playlist_id].append(stable_id)
    for playlist_id, name, deleted_at, updated_at, origin in conn.execute(
        "SELECT playlist_id, name, deleted_at, updated_at, origin_device_id FROM playlists"
    ):
        versions[(PLAYLISTS, playlist_id)] = Version(
            key=lww_key(updated_at, origin),
            content=(name, canonical_or_none(deleted_at), tuple(members[playlist_id])),
        )
    return versions


__all__ = [
    "PLAYLISTS",
    "TRACKS",
    "LwwKey",
    "LwwOracle",
    "RowKey",
    "Version",
    "canonical_or_none",
    "lww_key",
    "read_versions",
    "track_lifecycle_key",
]
