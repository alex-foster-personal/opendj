"""Bind analysis writes to the track's ``audio_hash`` at claim/start time.

Issue #6114: a folder rescan can replace bytes under a stable id while a
worker still holds an in-flight job. Comparing the hash at commit time closes
that window without re-hashing the file in the worker.
"""

from __future__ import annotations

import sqlite3


class StaleSourceBytesError(ValueError):
    """The track's on-disk bytes changed after this job started."""


def read_track_audio_hash(conn: sqlite3.Connection, stable_id: str) -> str | None:
    row = conn.execute(
        "SELECT audio_hash FROM tracks WHERE stable_id = ? AND deleted_at IS NULL",
        (stable_id,),
    ).fetchone()
    if row is None:
        return None
    value = row[0]
    return str(value) if value is not None else None


def assert_source_bytes_unchanged(
    conn: sqlite3.Connection,
    stable_id: str,
    claimed_audio_hash: str | None,
) -> None:
    """Refuse a write when ``tracks.audio_hash`` moved since the claim snapshot."""
    if claimed_audio_hash is None:
        return
    current = read_track_audio_hash(conn, stable_id)
    if current == claimed_audio_hash:
        return
    raise StaleSourceBytesError(
        f"track {stable_id!r} audio_hash changed from {claimed_audio_hash!r} "
        f"to {current!r} while analysis was in flight"
    )
