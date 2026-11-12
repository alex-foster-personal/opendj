"""Rows the hub already holds at the same digest (issue #10304).

Machine-local bookkeeping, never synced. A ``not_newer`` rejection means the
hub's copy is at least as fresh; recording the spoke's digest at that moment
lets the next round skip the row until it changes locally.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3

from apps.sync_hub import engine, protocol
from apps.sync_hub.engine_common import _pk_predicate
from apps.sync_hub.protocol import SPEC_BY_TABLE, RowChange, encode_row_pk
from apps.sync_hub.protocol_common import canonical_bytes, canonical_row
from apps.sync_hub.rejected_rows import REASON_NOT_NEWER, RejectedRow

log = logging.getLogger("apps.sync_hub.client")

ANSWERED_TABLE: str = "sync_hub_answered"


def ensure_answered_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {ANSWERED_TABLE} (
            hub_peer TEXT NOT NULL,
            table_name TEXT NOT NULL,
            row_pk TEXT NOT NULL,
            row_digest TEXT NOT NULL,
            PRIMARY KEY (hub_peer, table_name, row_pk)
        )
        """
    )


def _table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (ANSWERED_TABLE,),
    ).fetchone()
    return row is not None


def _row_digest(change: RowChange) -> str:
    digest = hashlib.sha256()
    digest.update(canonical_bytes(change.to_wire()))
    return digest.hexdigest()


def _row_digest_from_db(conn: sqlite3.Connection, table: str, pk: tuple[str, ...]) -> str | None:
    spec = SPEC_BY_TABLE.get(table)
    if spec is None:
        return None
    columns = protocol.table_columns(conn, table)
    row = conn.execute(
        f"SELECT {', '.join(columns)} FROM {table} WHERE {_pk_predicate(spec)}",
        pk,
    ).fetchone()
    if row is None:
        return None
    digest = hashlib.sha256()
    digest.update(canonical_bytes(canonical_row(table, columns, row)))
    return digest.hexdigest()


def _changelog_seq(conn: sqlite3.Connection, table: str, row_pk: str) -> int | None:
    row = conn.execute(
        "SELECT MAX(seq) FROM local_changelog WHERE table_name = ? AND row_pk = ?",
        (table, row_pk),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return int(row[0])


def load_answered(conn: sqlite3.Connection, hub_peer: str) -> dict[tuple[str, str], str]:
    if not _table_exists(conn):
        return {}
    return {
        (str(table_name), str(row_pk)): str(row_digest)
        for table_name, row_pk, row_digest in conn.execute(
            f"SELECT table_name, row_pk, row_digest FROM {ANSWERED_TABLE} WHERE hub_peer = ?",
            (hub_peer,),
        )
    }


def record_not_newer(
    conn: sqlite3.Connection,
    hub_peer: str,
    rejected_rows: tuple[RejectedRow, ...],
    *,
    offer_by_key: dict[tuple[str, str], RowChange],
    accepted: int = 0,
    rejected: int = 0,
) -> None:
    """Persist hub-answered rows at their current digests."""
    named_not_newer = [row for row in rejected_rows if row.reason == REASON_NOT_NEWER]
    batch_all_not_newer = (
        accepted == 0
        and rejected > 0
        and rejected == len(offer_by_key)
        and (not rejected_rows or all(row.reason == REASON_NOT_NEWER for row in rejected_rows))
    )
    if batch_all_not_newer:
        to_record: list[tuple[str, str, RowChange | None, tuple[str, ...]]] = [
            (table, row_pk, change, change.pk) for (table, row_pk), change in offer_by_key.items()
        ]
    elif named_not_newer:
        to_record = [
            (row.table, encode_row_pk(row.pk), offer_by_key.get((row.table, encode_row_pk(row.pk))), row.pk)
            for row in named_not_newer
        ]
    else:
        return
    ensure_answered_table(conn)
    for table, row_pk, change, pk in to_record:
        digest = _row_digest(change) if change is not None else _row_digest_from_db(conn, table, pk)
        if digest is None:
            continue
        conn.execute(
            f"""
            INSERT INTO {ANSWERED_TABLE}(hub_peer, table_name, row_pk, row_digest)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(hub_peer, table_name, row_pk) DO UPDATE SET
                row_digest = excluded.row_digest
            """,
            (hub_peer, table, row_pk, digest),
        )


def forget_hub(conn: sqlite3.Connection, hub_peer: str) -> None:
    """Drop every answer from ``hub_peer``: a restored hub may no longer hold those rows."""
    if _table_exists(conn):
        conn.execute(f"DELETE FROM {ANSWERED_TABLE} WHERE hub_peer = ?", (hub_peer,))


def filter_offer(
    conn: sqlite3.Connection,
    hub_peer: str,
    offer: engine.Offer,
) -> tuple[engine.Offer, int]:
    """Drop rows the hub already answered at the same digest."""
    answered = load_answered(conn, hub_peer)
    if not answered:
        return offer, 0
    kept: list[RowChange] = []
    skipped: list[engine.HeldRow] = []
    for change in offer.rows:
        key = (change.table, change.row_pk)
        digest = _row_digest(change)
        if answered.get(key) == digest:
            seq = _changelog_seq(conn, change.table, change.row_pk)
            skipped.append(
                engine.HeldRow(
                    table=change.table,
                    pk=change.pk,
                    seq=seq,
                    settled=True,
                )
            )
            continue
        kept.append(change)
    if not kept and skipped:
        log.info(
            "hub already has %d offered row(s) at the same digest; no push",
            len(skipped),
        )
    return engine.Offer.of(kept, (*offer.held, *skipped, *offer.settled)), len(skipped)


__all__ = [
    "ANSWERED_TABLE",
    "ensure_answered_table",
    "filter_offer",
    "forget_hub",
    "load_answered",
    "record_not_newer",
]
