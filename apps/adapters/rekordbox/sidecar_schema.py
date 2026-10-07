"""The rekordbox vendor sidecar tables: hot-cue reversal tokens and slot generations.

Supersedes: the vendor sidecar DDL in ``apps.engine_core.store.schema``, which now
re-exports it from here.

ONE home for this DDL. It used to sit in ``apps.engine_core.store.schema`` with the
note "declared here so the DDL has one home", but it is NOT part of the state-DB
ladder: legacy creates these inside the rekordbox vendor DB (master.db), never
state.db, and the only writer is ``apps.adapters.rekordbox.reversal``. Homing it
with that adapter (round 12 of the SMARTEST-CI counter) removes the last import an
adapter made of the engine, the edge that kept ``adapters`` inside the 31-scope
import cycle. ``apps.engine_core.store.schema`` re-exports all three names.
Legacy source: apps/webui/server/rb_vendor.py (_ensure_reversal_tables).
"""

from __future__ import annotations

import sqlite3

VENDOR_SIDECAR_DDL: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS rb_hot_cue_reversal ("
    "ID TEXT PRIMARY KEY, ContentID TEXT NOT NULL, Kind INTEGER NOT NULL, "
    "PreimageJson TEXT, PostRevision TEXT NOT NULL, CreatedAt TEXT NOT NULL, "
    "ConsumedAt TEXT)",
    "CREATE TABLE IF NOT EXISTS rb_hot_cue_slot_revision ("
    "ContentID TEXT NOT NULL, Kind INTEGER NOT NULL, Generation INTEGER NOT NULL, "
    "PRIMARY KEY (ContentID, Kind))",
)

VENDOR_SIDECAR_TABLES: tuple[str, ...] = (
    "rb_hot_cue_reversal",
    "rb_hot_cue_slot_revision",
)


def ensure_vendor_sidecar_tables(conn: sqlite3.Connection) -> None:
    """Create the rekordbox-side hot-cue reversal tables. Idempotent.

    ``conn`` must be the VENDOR database (rekordbox ``master.db``), never the
    engine state DB -- these rows are keyed by rekordbox ContentID and only
    mean anything next to the vendor tables they reverse.
    """
    for stmt in VENDOR_SIDECAR_DDL:
        conn.execute(stmt)


__all__ = ["VENDOR_SIDECAR_DDL", "VENDOR_SIDECAR_TABLES", "ensure_vendor_sidecar_tables"]
