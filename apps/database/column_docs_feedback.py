"""Column docs for the v11 ``feedback_pins`` table (FBSYNC-01, ADR-0013).

Own module for the same reason ``column_docs_lyrics`` is one: the combined
column-doc table would otherwise cross the 600-line file-size gate.
"""
from __future__ import annotations

FEEDBACK_COLUMN_DOCS: dict[str, dict[str, str]] = {
    "feedback_pins": {
        "pin_id": (
            "Primary key: the 12-hex pin id minted when the pin was dropped. "
            "The SAME id on two machines is the same pin, so stores that "
            "held a copy each collapse to one row."
        ),
        "doc": (
            "The whole pin as JSON, in the exact shape the feedback router's "
            "CommentOut model returns (CHECK json_valid). Includes status, "
            "agent_note, issue_url, author, the build stamp, the environment "
            "(machine name, UI kind, viewport, release) and attachment "
            "METADATA. Attachment BYTES do not sync (FBSYNC-05)."
        ),
        "updated_at": (
            "NOT NULL. The pin's own last-edit time (its updated_at, else its "
            "created_at), canonical UTC. What last-writer-wins orders on, so "
            "it must be the edit time and never the sync time."
        ),
        "origin_device_id": (
            "machine_id of the machine whose edit this row carries. The LWW "
            "tiebreak when two machines stamped the same instant."
        ),
        "deleted_at": (
            "Archive tombstone. Set when the pin was archived on any machine; "
            "the doc then carries status 'archived'. Never inferred from a "
            "pin being missing from a comments.json."
        ),
    },
}

__all__ = ["FEEDBACK_COLUMN_DOCS"]
