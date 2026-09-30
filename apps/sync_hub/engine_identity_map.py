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
import time
from collections import ChainMap
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from apps.shared.state import db as state_db
from apps.sync_hub import protocol
from apps.sync_hub.engine_identity import _follow_remap, remap_track_children
from apps.sync_hub.sync_set import identity_duplicate_remap

REMAP_TABLE: str = "sync_identity_remap"

#: Loser/survivor pairs remapped per write transaction. Matches
#: ``client.PUSH_BATCH_ROWS`` in spirit: a 900+ row backlog (issue #3251)
#: must never hold one write transaction for its whole length, or a single
#: SQLITE_BUSY mid-backlog rolls back every already-remapped row too.
IDENTITY_REMAP_BATCH_ROWS: int = 200

#: Backoff between retries once a batch's own BEGIN IMMEDIATE has already
#: waited out busy_timeout and still found the writer lock held. This is the
#: honest outer guard, not the fix -- batching above is what stops one
#: contended batch from re-doing the whole backlog's work.
_BUSY_RETRY_BACKOFFS_S: tuple[float, ...] = (0.25, 0.5, 1.0)

#: Patchable seam: tests hook this to release a lock-holder deterministically
#: on retry instead of racing real wall-clock sleeps.
_sleep = time.sleep


@dataclass(frozen=True)
class IdentityRepairRequest:
    """One bounded identity repair: fetch the hub survivor, offer the loser once."""

    hub_survivor_pk: str
    offer_pk: str


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
    combined = PersistedRemap.load(conn).with_local(identity_duplicate_remap(conn))
    return {
        loser: _follow_remap(combined, mapped)
        for loser, mapped in combined.items()
        if loser != _follow_remap(combined, mapped)
    }


@dataclass(frozen=True)
class PersistedRemap:
    """The persisted hub remaps, chain-normalized, and every PK they name."""

    combined: dict[str, str]
    hub_domain: frozenset[str]

    @classmethod
    def load(cls, conn: sqlite3.Connection) -> PersistedRemap:
        persisted = load_identity_remap(conn)
        combined = {
            loser: _follow_remap(persisted, survivor)
            for loser, survivor in persisted.items()
        }
        return cls(combined, frozenset(combined) | frozenset(combined.values()))

    def with_local(self, local: Mapping[str, str]) -> dict[str, str]:
        """These remaps plus every local election outside the hub domain."""
        combined = dict(self.combined)
        combined.update(self._local_outside_hub_domain(local))
        return combined

    def effective_losers(
        self, local: Mapping[str, str], candidates: Iterable[str]
    ) -> frozenset[str]:
        """The ``candidates`` in ``effective_identity_remap(conn)``, given ``local``
        holds every election their remap chains can reach."""
        combined = ChainMap(self._local_outside_hub_domain(local), self.combined)
        return frozenset(
            pk
            for pk in candidates
            if (mapped := combined.get(pk)) is not None
            and pk != _follow_remap(combined, mapped)
        )

    def _local_outside_hub_domain(self, local: Mapping[str, str]) -> dict[str, str]:
        return {
            loser: survivor
            for loser, survivor in local.items()
            if loser not in self.hub_domain and survivor not in self.hub_domain
        }


def _remove_remap_loser(
    conn: sqlite3.Connection, remap: dict[str, str], loser_pk: str
) -> None:
    """Drop one persisted remap row and keep the in-memory map aligned."""
    if loser_pk in remap:
        del remap[loser_pk]
    ensure_identity_remap_table(conn)
    conn.execute(
        f"DELETE FROM {REMAP_TABLE} WHERE loser_pk = ?",
        (loser_pk,),
    )


def apply_hub_identity_rejects(
    conn: sqlite3.Connection,
    rejects: Sequence[protocol.IdentityReject],
) -> tuple[int, tuple[IdentityRepairRequest, ...]]:
    """Apply hub-authority collapse remaps after push.

    Hub ``offered_pk`` and ``survivor_pk`` are raw verdicts: never chain-follow
    the local table before reconciling, or a reversed row makes the reject a
    no-op. Returns how many rejects landed and bounded repair requests for the
    same sync round.
    """
    if not rejects:
        return 0, ()
    remap = load_identity_remap(conn)
    applied = 0
    repairs: list[IdentityRepairRequest] = []
    for reject in rejects:
        if reject.table != "tracks":
            raise protocol.SyncProtocolError(
                f"identity reject for unsupported table {reject.table!r}"
            )
        offered = reject.offered_pk
        survivor = reject.survivor_pk
        if not offered or not survivor:
            raise protocol.SyncProtocolError(
                "identity reject must name non-empty offered_pk and survivor_pk"
            )
        if offered == survivor:
            raise protocol.SyncProtocolError(
                "identity reject offered_pk and survivor_pk must differ"
            )
        _remove_remap_loser(conn, remap, survivor)
        for loser, mapped in list(remap.items()):
            if loser == offered:
                continue
            if mapped == offered:
                record_identity_remap(conn, remap, loser, survivor)
        record_identity_remap(conn, remap, offered, survivor)
        remap_track_children(conn, offered, survivor)
        applied += 1
        repairs.append(
            IdentityRepairRequest(hub_survivor_pk=survivor, offer_pk=offered)
        )
    return applied, tuple(repairs)


def _busy_timeout_ms(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA busy_timeout").fetchone()
    return int(row[0]) if row is not None else 0


def _commit_remap_batch(
    conn: sqlite3.Connection,
    batch: Sequence[tuple[str, str]],
    *,
    batch_index: int,
    batch_count: int,
) -> None:
    """Remap one bounded batch of loser/survivor pairs in its own write transaction.

    ``BEGIN IMMEDIATE`` claims the write lock up front, so a busy database
    fails here -- before any row in this batch moves -- rather than partway
    through. SQLite's own ``busy_timeout`` already waits out the lock once;
    a :class:`sqlite3.OperationalError` reaching this function means that
    wait was exhausted. Retrying with backoff on top of that is a second,
    honest wait for the SAME cause (issue #3251: self-contention, a writer
    elsewhere in this process), never a substitute for the batching above.
    """
    attempts = len(_BUSY_RETRY_BACKOFFS_S) + 1
    for attempt in range(1, attempts + 1):
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if not state_db.is_sqlite_busy(exc):
                # Not contention (e.g. a caller-owned transaction): surface it as
                # itself rather than as a lock holder that does not exist.
                raise
            if attempt == attempts:
                raise state_db.StateStoreBusyError(
                    "STATE_DB_BUSY: prepare_spoke_identity could not acquire "
                    f"the state DB write lock for remap batch {batch_index}/"
                    f"{batch_count} ({len(batch)} rows) after {attempt} "
                    f"attempt(s) and busy_timeout={_busy_timeout_ms(conn)}ms "
                    "per attempt; the lock holder is another connection "
                    f"inside this same process (self-contention, not a peer "
                    f"process): {exc}"
                ) from exc
            _sleep(_BUSY_RETRY_BACKOFFS_S[attempt - 1])
            continue
        try:
            for loser, survivor in batch:
                remap_track_children(conn, loser, survivor)
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        return


def prepare_spoke_identity(conn: sqlite3.Connection) -> int:
    """Remap children of local content-identity losers onto the survivor.

    Does not drop the loser ``tracks`` row: the app still sees it, and
    :mod:`apps.sync_hub.sync_set` holds it out of the offer and the digest.
    Commits in batches of :data:`IDENTITY_REMAP_BATCH_ROWS` pairs (issue
    #3251): a 900+ row backlog never holds one write transaction across the
    whole remap, so a mid-backlog lock conflict costs one batch's work, not
    the round, and WAL checkpoints get a chance to run between batches.
    Returns how many loser PKs were remapped.
    """
    remap = list(effective_identity_remap(conn).items())
    batch_count = -(-len(remap) // IDENTITY_REMAP_BATCH_ROWS) if remap else 0
    for index in range(batch_count):
        start = index * IDENTITY_REMAP_BATCH_ROWS
        batch = remap[start : start + IDENTITY_REMAP_BATCH_ROWS]
        _commit_remap_batch(conn, batch, batch_index=index + 1, batch_count=batch_count)
    return len(remap)


__all__ = [
    "IDENTITY_REMAP_BATCH_ROWS",
    "IdentityRepairRequest",
    "PersistedRemap",
    "REMAP_TABLE",
    "_remove_remap_loser",
    "apply_hub_identity_rejects",
    "effective_identity_remap",
    "ensure_identity_remap_table",
    "load_identity_remap",
    "prepare_spoke_identity",
    "record_identity_remap",
]
