"""Shared Rekordbox DB teardown for read-only audit CLIs."""

from __future__ import annotations


def close_db_quietly(db) -> None:
    """Close a pyrekordbox handle. Close failures propagate to the caller."""
    db.close()


__all__ = ["close_db_quietly"]
