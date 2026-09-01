"""The ``machines`` registry merge: owner-scoped writes, no wall-clock gate.

Split out of :mod:`apps.sync_hub.engine` (quality-gate file_size ratchet,
round 4). See :func:`merge_machines` for the round 3 finding R1 shape this
enforces: a snapshot may create a machine it has never met, and may refresh
the row belonging to whoever sent it, but it may not rewrite anyone else's.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from apps.sync_hub.engine_common import SyncApplyError
from apps.sync_hub.protocol import MachineRow

# ----- machines registry -----------------------------------------------------


def upsert_machine(conn: sqlite3.Connection, machine: MachineRow) -> bool:
    """Insert or overwrite one machine's OWN row unconditionally. True if changed.

    This is the write a machine makes about ITSELF: the owner-scoped branch of
    :func:`merge_machines`, driven by the ``machine_id`` that authored the
    snapshot. Its owner is authoritative about its own
    ``name`` / ``platform`` / ``is_hub`` / ``data_root``, so there is no LWW
    and -- since round 3 finding R1 -- no ``last_seen`` wall-clock gate. That
    gate was the write primitive: it let any peer with a fast clock rewrite
    every OTHER machine's registry row, because ``last_seen`` is stamped from
    the local wall clock and nothing checked that the pusher WAS the machine
    it described. Every other machine's row now takes the
    :func:`_insert_machine_if_absent` path instead, so this unconditional
    overwrite only ever touches the caller's own row.
    """
    before = conn.total_changes
    conn.execute(
        """
        INSERT INTO machines(
            machine_id, name, platform, is_hub, data_root, first_seen, last_seen
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(machine_id) DO UPDATE SET
            name      = excluded.name,
            platform  = excluded.platform,
            is_hub    = excluded.is_hub,
            data_root = excluded.data_root,
            last_seen = excluded.last_seen
        """,
        (
            machine.machine_id,
            machine.name,
            machine.platform,
            1 if machine.is_hub else 0,
            machine.data_root,
            machine.first_seen,
            machine.last_seen,
        ),
    )
    return conn.total_changes > before


def _insert_machine_if_absent(
    conn: sqlite3.Connection, machine: MachineRow
) -> bool:
    """Create a peer's row if this DB has never met it; never rewrite one.

    A snapshot from a peer is allowed to TEACH this DB about machines it has
    not met -- the FK parents from ``sync_policies`` / ``playlist_pins`` /
    ``track_locations`` must land, which is the round 2 finding N4 fix and has
    to survive -- but it must not be able to REWRITE a row it does not own
    (round 3 finding R1). ``INSERT OR IGNORE`` is exactly that: it inserts an
    unknown machine and does nothing for a known one. It also swallows a
    ``machines.name`` UNIQUE collision arriving in the snapshot (round 3
    finding R1a): the machine such a forged name would lock out is not the one
    that sent the snapshot, so the row is simply not applied rather than
    raising out of ``hello`` / ``push`` and bricking an innocent third machine.
    """
    before = conn.total_changes
    conn.execute(
        """
        INSERT OR IGNORE INTO machines(
            machine_id, name, platform, is_hub, data_root, first_seen, last_seen
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            machine.machine_id,
            machine.name,
            machine.platform,
            1 if machine.is_hub else 0,
            machine.data_root,
            machine.first_seen,
            machine.last_seen,
        ),
    )
    return conn.total_changes > before


def machines_snapshot(conn: sqlite3.Connection) -> list[MachineRow]:
    """Every known machine, ordered by id so the payload is deterministic."""
    cursor = conn.execute(
        "SELECT machine_id, name, platform, is_hub, data_root, first_seen, last_seen "
        "FROM machines ORDER BY machine_id"
    )
    return [
        MachineRow(
            machine_id=str(row[0]),
            name=str(row[1]),
            platform=str(row[2]),
            is_hub=bool(row[3]),
            data_root=None if row[4] is None else str(row[4]),
            first_seen=str(row[5]),
            last_seen=str(row[6]),
        )
        for row in cursor
    ]


def merge_machines(
    conn: sqlite3.Connection, machines: Iterable[MachineRow], *, caller_id: str
) -> int:
    """Apply a registry snapshot under owner-scoped rules. Returns rows changed.

    ``caller_id`` is the machine that AUTHORED this snapshot -- the pusher on
    ``push``, the spoke on its ``hello``, the hub on the snapshot a spoke
    pulls. That machine is authoritative about ITS OWN row, so its row is
    upserted (:func:`upsert_machine`); every OTHER row is
    :func:`_insert_machine_if_absent`, created only if this DB has never met
    it and never rewritten. That is the round 3 finding R1 fix: the old merge
    let a peer's snapshot rewrite any machine's ``name`` / ``platform`` /
    ``is_hub`` / ``data_root`` under a bare ``last_seen`` wall-clock gate, so
    one forged push could rename a victim, and a forged ``name`` collision
    could raise out of a THIRD machine's own ``hello`` and lock it out (R1a).

    A ``machines.name`` collision on the CALLER'S OWN row is a different
    matter -- two machines genuinely sharing a hostname (finding A5) -- and
    still raises :class:`SyncApplyError`, because the machine that hits it is
    the one that caused it.
    """
    changed = 0
    for machine in machines:
        if machine.machine_id == caller_id:
            try:
                if upsert_machine(conn, machine):
                    changed += 1
            except sqlite3.IntegrityError as exc:
                raise SyncApplyError(
                    f"machines row {machine.machine_id} ({machine.name!r}) "
                    f"conflicts with a local row: {exc}. machines.name is "
                    f"UNIQUE, so two machines sharing a hostname collide here "
                    f"rather than silently overwriting each other."
                ) from exc
        elif _insert_machine_if_absent(conn, machine):
            changed += 1
    return changed


__all__ = [
    "machines_snapshot",
    "merge_machines",
    "upsert_machine",
]
