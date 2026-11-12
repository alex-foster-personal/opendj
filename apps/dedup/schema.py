"""The dedup database schema, as code inside ``apps.dedup`` uses it.

The DDL moved verbatim to :mod:`apps.dedup.contract.schema` (M1,
ADR-NEW-module-contracts) because ``apps.tags`` writes ``tag_provenance`` into
the same database and may import only dedup's contract. The contract's
``ensure_schema`` returns nothing, since an open connection never crosses a
contract; inside ``apps.dedup`` this module's ``ensure_schema`` still returns
the migrated connection, migrating that same connection so an in-memory
database (``:memory:``), which lives only as long as its connection, keeps the
tables. The two migrations must agree: ``tests/dedup`` holds them to it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from apps.dedup.contract.schema import SCHEMA_SQL

__all__ = ["SCHEMA_SQL", "ensure_schema"]


def ensure_schema(db_path: Path) -> sqlite3.Connection:
    """Create (or migrate) the dedup schema; return an open connection."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_SQL)
    cluster_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(duplicate_clusters)")}
    if "flagged_manual_review" not in cluster_columns:
        conn.execute("ALTER TABLE duplicate_clusters ADD COLUMN flagged_manual_review INTEGER NOT NULL DEFAULT 0")
    if "basis" not in cluster_columns:
        conn.execute("ALTER TABLE duplicate_clusters ADD COLUMN basis TEXT NOT NULL DEFAULT 'fingerprint'")
    conn.commit()
    return conn
