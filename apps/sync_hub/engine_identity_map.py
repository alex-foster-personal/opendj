"""Identity-collapse remaps that have to outlive one apply batch.

``rewrite_incoming_change`` only sees remaps from the CURRENT ``hub_apply``.
A first-sync splits tracks and playlists across HTTP batches
(``PUSH_BATCH_ROWS`` is 200), so a playlist naming a collapse loser 409s
with FOREIGN KEY. This table is bookkeeping, not in the sync set.

Spoke-side, content-identity duplicates in ONE library have to remap their
children onto the LWW survivor BEFORE the offer: otherwise the local digest
still hashes memberships that name the loser, the hub hashes the remapped
bundle, and ADR 04 c6 fires on a library that is not corrupt.
"""
from __future__ import annotations

import sqlite3

from apps.sync_hub.engine_identity import remap_track_children
from apps.sync_hub.sync_set import identity_duplicate_remap

REMAP_TABLE: str = "sync_identity_remap"


def ensure_identity_remap_table(conn: sqlite3.Connection) -> None:
    """Create the local remap table. Idempotent."""
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {REMAP_TABLE} (
            loser_pk TEXT PRIMARY KEY,
            survivor_pk TEXT NOT NULL
        )
        """
    )


def load_identity_remap(conn: sqlite3.Connection) -> dict[str, str]:
    """Loser PK -> survivor PK, including remaps from earlier batches."""
    ensure_identity_remap_table(conn)
    return {
        str(loser): str(survivor)
        for loser, survivor in conn.execute(
            f"SELECT loser_pk, survivor_pk FROM {REMAP_TABLE}"
        )
    }


def record_identity_remap(
    conn: sqlite3.Connection,
    remap: dict[str, str],
    loser: str,
    survivor: str,
) -> None:
    """Remember ``loser -> survivor`` for this batch and every later one."""
    if not loser or loser == survivor:
        return
    remap[loser] = survivor
    ensure_identity_remap_table(conn)
    conn.execute(
        f"INSERT INTO {REMAP_TABLE}(loser_pk, survivor_pk) VALUES (?, ?) "
        "ON CONFLICT(loser_pk) DO UPDATE SET survivor_pk = excluded.survivor_pk",
        (loser, survivor),
    )


def prepare_spoke_identity(conn: sqlite3.Connection) -> int:
    """Remap children of local content-identity losers onto the survivor.

    Does not drop the loser ``tracks`` row: the app still sees it, and
    :mod:`apps.sync_hub.sync_set` holds it out of the offer and the digest.
    Returns how many loser PKs were remapped.
    """
    remap = identity_duplicate_remap(conn)
    for loser, survivor in remap.items():
        remap_track_children(conn, loser, survivor)
    return len(remap)


__all__ = [
    "REMAP_TABLE",
    "ensure_identity_remap_table",
    "load_identity_remap",
    "prepare_spoke_identity",
    "record_identity_remap",
]
