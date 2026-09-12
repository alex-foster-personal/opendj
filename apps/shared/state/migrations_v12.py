"""Migration ladder (v12) for the shared state schema: synced feedback pins.

Sixth module in the split ladder (see :mod:`apps.shared.state.migrations_v10`
for why the ladder is split). :data:`apps.shared.state.schema.MIGRATIONS`
assembles every part in order and ``schema.apply_migrations`` is the runner.

Step 11 -> 12 adds ``feedback_pins``: one synced row per Open DJ feedback
comment pin, so the same pins appear on every machine enrolled to the same
hub (requirement FBSYNC-01, ADR-0013). Pin 4bb2dc57bb5e ("did we wipe all the
old feedback pins?") is the defect it answers: on Fri 11 Sep 2026 four
separate ``comments.json`` stores held 138 outstanding pins between them and
each machine could see only its own.

Four deliberate readings:

1. ``doc`` holds the WHOLE pin as JSON (id, page, anchor, x/y, text, status,
   agent_note, issue_url, author, build stamp, environment including the
   machine name, attachment metadata). The pin's shape is owned by the
   feedback router's ``CommentOut`` model and has grown four times since
   Mon 31 Aug 2026; mirroring each field as a column would make every future
   pin field a schema migration and a lockstep hub upgrade. The row is the
   unit of replication, exactly as a ``tracks`` row is, so nothing is lost by
   carrying it as one value. ``CHECK (json_valid(doc))`` refuses a peer that
   offers anything else.
2. ``updated_at TEXT NOT NULL``, matching ``tracks`` and ``lyric_verdict``:
   a pin always has an edit time (its ``updated_at``, else its
   ``created_at``), so a NULL-stamped offer is a protocol violation that
   ``engine_apply._upsert`` refuses loudly rather than storing a row that
   would read as epoch-old and lose every conflict.
3. Archive is a TOMBSTONE: ``deleted_at`` is set and the doc carries
   ``status: archived``. It is never inferred from a pin being absent from a
   ``comments.json``. The row is never hard-deleted
   (``tests/cloudsync/test_soft_delete.py`` enforces it repo-wide).
4. No foreign key. A pin references no other synced row, so the table needs
   no ``apps.sync_hub.sync_set.PARENT_KEYS`` entry and can never be held
   back by a quarantined parent.
"""
from __future__ import annotations

FEEDBACK_PINS_TABLE: str = "feedback_pins"

_V12: list[str] = [
    # The sync trio LAST and declared character-for-character as ``tracks``
    # declares it, like every other synced table in this ladder.
    """
    CREATE TABLE feedback_pins (
        pin_id           TEXT PRIMARY KEY CHECK (length(pin_id) > 0),
        doc              TEXT NOT NULL CHECK (json_valid(doc)),
        updated_at       TEXT NOT NULL,
        origin_device_id TEXT,
        deleted_at       TEXT
    )
    """,
]

__all__ = ["FEEDBACK_PINS_TABLE", "_V12"]
