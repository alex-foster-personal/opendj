"""Changelog retention: drop superseded entries, never lose coverage.

Split out of :mod:`apps.sync_hub.engine` (quality-gate file_size ratchet,
round 4).
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from apps.shared.state import sync_stamp
from apps.sync_hub.engine_common import (
    CHANGELOG_TABLES,
    DEFAULT_KEEP_DAYS,
    DEFAULT_KEEP_ROWS,
    HUB_CHANGELOG_TABLE,
    SyncApplyError,
)

# ----- retention ---------------------------------------------------------


def prune_changelog(
    conn: sqlite3.Connection,
    *,
    changelog: str = HUB_CHANGELOG_TABLE,
    keep_days: float = DEFAULT_KEEP_DAYS,
    keep_rows: int = DEFAULT_KEEP_ROWS,
    now: str | None = None,
) -> int:
    """Drop SUPERSEDED changelog entries. Returns how many went.

    Neither changelog had any retention at all (round 2 finding N6): one row
    per synced-table write, forever, on a library the repo measures at 8355
    tracks with many analyzed fields each.

    What makes this safe is that it only ever deletes an entry that is NOT
    the newest for its ``(table_name, row_pk)``. Both changelogs are read by
    :func:`apps.sync_hub.engine_changes._changelog_rows`, which reads each
    row LIVE and collapses several entries for one row into one change, so a
    superseded entry carries no information the newest one does not.
    Coverage is therefore unchanged: a spoke pulling from seq 0 still gets
    every row that ever changed, and ``MAX(seq)`` cannot move, so the prune
    cannot look like a hub restore (:mod:`apps.sync_hub.generation`).

    ``keep_days`` and ``keep_rows`` are belt on top of that: an entry has to
    be superseded AND older than ``keep_days`` AND outside the newest
    ``keep_rows`` entries before it is eligible. Both are compared as the
    protocol compares -- ``received_at`` lexicographically, which is an
    ordering over instants because every writer emits the canonical UTC
    format (``apps.shared.state.sync_stamp.CANONICAL_FORMAT``).
    """
    if changelog not in CHANGELOG_TABLES:
        raise SyncApplyError(
            f"{changelog!r} is not a changelog table; expected one of "
            f"{sorted(CHANGELOG_TABLES)}."
        )
    if keep_days < 0:
        raise SyncApplyError(f"keep_days must be >= 0, got {keep_days}")
    if keep_rows < 0:
        raise SyncApplyError(f"keep_rows must be >= 0, got {keep_rows}")
    cutoff_at = (
        datetime.now(UTC) if now is None else sync_stamp.parse_canonical(now)
    ) - timedelta(days=keep_days)
    cursor = conn.execute(
        f"""
        DELETE FROM {changelog}
        WHERE seq NOT IN (
            SELECT MAX(seq) FROM {changelog} GROUP BY table_name, row_pk
        )
          AND received_at < ?
          AND seq <= (SELECT COALESCE(MAX(seq), 0) FROM {changelog}) - ?
        """,
        (sync_stamp.canonical_from(cutoff_at), int(keep_rows)),
    )
    return int(cursor.rowcount)


__all__ = ["prune_changelog"]
