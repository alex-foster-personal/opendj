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
from collections.abc import Sequence

from apps.sync_hub import protocol
from apps.sync_hub.engine_identity import _follow_remap, remap_track_children
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


def _identity_remap_table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (REMAP_TABLE,)
    ).fetchone()
    return row is not None


def load_identity_remap(conn: sqlite3.Connection) -> dict[str, str]:
    """Loser PK -> survivor PK, including remaps from earlier batches.

    Read-only: the status route reads the sync set over a read-only
    connection, and even ``CREATE TABLE IF NOT EXISTS`` is a write to
    SQLite. A database that never recorded a remap has no rows to load.
    """
    if not _identity_remap_table_exists(conn):
        return {}
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


def effective_identity_remap(conn: sqlite3.Connection) -> dict[str, str]:
    """Loser PK -> survivor PK, merging local election with persisted hub remaps.

    Persisted ``sync_identity_remap`` rows are authoritative for every PK they
    name. Local LWW elections apply only outside that hub domain. Every value
    is chain-normalized through
    :func:`apps.sync_hub.engine_identity._follow_remap`.
    """
    local = identity_duplicate_remap(conn)
    persisted = load_identity_remap(conn)
    if not persisted:
        return {
            loser: _follow_remap(local, survivor)
            for loser, survivor in local.items()
            if loser != _follow_remap(local, survivor)
        }
    combined = {
        loser: _follow_remap(persisted, survivor)
        for loser, survivor in persisted.items()
    }
    hub_domain = set(combined.keys()) | set(combined.values())
    for loser, survivor in local.items():
        if loser in hub_domain or survivor in hub_domain:
            continue
        combined[loser] = survivor
    return {
        loser: _follow_remap(combined, mapped)
        for loser, mapped in combined.items()
        if loser != _follow_remap(combined, mapped)
    }


def apply_hub_identity_rejects(
    conn: sqlite3.Connection,
    rejects: Sequence[protocol.IdentityReject],
) -> int:
    """Apply hub-authority collapse remaps after push. Returns count applied."""
    if not rejects:
        return 0
    remap = load_identity_remap(conn)
    applied = 0
    for reject in rejects:
        if reject.table != "tracks":
            raise protocol.SyncProtocolError(
                f"identity reject for unsupported table {reject.table!r}"
            )
        offered = _follow_remap(remap, reject.offered_pk)
        survivor = _follow_remap(remap, reject.survivor_pk)
        if offered == survivor:
            continue
        for loser, mapped in list(remap.items()):
            if loser == offered:
                continue
            if mapped == offered:
                record_identity_remap(conn, remap, loser, survivor)
        record_identity_remap(conn, remap, offered, survivor)
        remap_track_children(conn, offered, survivor)
        applied += 1
    return applied


def prepare_spoke_identity(conn: sqlite3.Connection) -> int:
    """Remap children of local content-identity losers onto the survivor.

    Does not drop the loser ``tracks`` row: the app still sees it, and
    :mod:`apps.sync_hub.sync_set` holds it out of the offer and the digest.
    Returns how many loser PKs were remapped.
    """
    remap = effective_identity_remap(conn)
    for loser, survivor in remap.items():
        remap_track_children(conn, loser, survivor)
    return len(remap)


__all__ = [
    "REMAP_TABLE",
    "apply_hub_identity_rejects",
    "effective_identity_remap",
    "ensure_identity_remap_table",
    "load_identity_remap",
    "prepare_spoke_identity",
    "record_identity_remap",
]
