"""Docs for the v21 ``sync_write_tokens`` table (issue #4396, CloudSync digest gate).

Own module for the same reason ``column_docs_feedback`` is one: the combined
column-doc table would otherwise grow past the 600-line file-size gate.
"""

from __future__ import annotations

SYNC_GATE_TABLE_DOC: str = (
    "Machine-local, never synced. One row per table the CloudSync digest "
    "hashes, holding a random token that a trigger on that table replaces on "
    "every row INSERT, UPDATE or DELETE (migration v21). "
    "apps.sync_hub.digest_gate reuses a previous digest only while every "
    "token, both changelog seqs, the schema and the identity remap are "
    "unchanged, so a no-op sync does not re-hash the library."
)

SYNC_GATE_COLUMN_DOCS: dict[str, dict[str, str]] = {
    "sync_write_tokens": {
        "table_name": (
            "Primary key: the digested table this token watches, one of "
            "apps.sync_hub.sync_set.FK_ORDER."
        ),
        "token": (
            "NOT NULL. 16 random bytes, replaced by that table's write "
            "triggers on every row write. Random rather than a counter so a "
            "rolled-back write can never hand its value to a later, different "
            "write. Meaningless on its own; only equality matters."
        ),
    },
}

__all__ = ["SYNC_GATE_COLUMN_DOCS", "SYNC_GATE_TABLE_DOC"]
