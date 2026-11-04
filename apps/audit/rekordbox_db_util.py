"""Shared Rekordbox DB teardown for read-only audit CLIs."""

from __future__ import annotations


def close_db_quietly(db) -> None:
    """Close a pyrekordbox handle without raising during CLI teardown."""
    try:
        db.close()
    except Exception:
        pass


__all__ = ["close_db_quietly"]
