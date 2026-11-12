"""Write audio-hash duplicate pairs into the dedup cluster tables at import time."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .schema import ensure_schema

_AUDIO_HASH_RATIONALE = "audio_hash"
_AUDIO_HASH_SIMILARITY = 1.0


def record_hash_duplicate(
    conn: sqlite3.Connection,
    new_stable_id: str,
    existing_stable_id: str,
    audio_hash: str,
    *,
    new_path: str,
    existing_path: str,
) -> bool:
    """Record one content duplicate (same ``audio_hash``, different paths).

    Returns True only when a new cluster or alias row was written. Idempotent
    when the pair is already linked under rationale ``audio_hash``.
    """
    if not audio_hash or new_stable_id == existing_stable_id:
        return False
    if _pair_recorded(conn, new_stable_id, existing_stable_id):
        return False
    cluster_id = _cluster_for_member(conn, existing_stable_id)
    if cluster_id is None:
        cur = conn.execute(
            "INSERT INTO duplicate_clusters "
            "(canonical_stable_id, canonical_path, rationale, flagged_manual_review) "
            "VALUES (?, ?, ?, 0)",
            (existing_stable_id, existing_path, _AUDIO_HASH_RATIONALE),
        )
        cluster_id = int(cur.lastrowid or 0)
    conn.execute(
        "INSERT OR REPLACE INTO track_aliases "
        "(alias_stable_id, alias_path, cluster_id, canonical_stable_id, similarity) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            new_stable_id,
            new_path,
            cluster_id,
            _canonical_for_cluster(conn, cluster_id, existing_stable_id),
            _AUDIO_HASH_SIMILARITY,
        ),
    )
    return True


def _pair_recorded(conn: sqlite3.Connection, new_stable_id: str, existing_stable_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM track_aliases AS a "
        "JOIN duplicate_clusters AS c ON c.cluster_id = a.cluster_id "
        "WHERE c.rationale = ? AND ("
        " (a.alias_stable_id = ? AND a.canonical_stable_id = ?) OR "
        " (a.alias_stable_id = ? AND a.canonical_stable_id = ?)"
        ")",
        (_AUDIO_HASH_RATIONALE, new_stable_id, existing_stable_id, existing_stable_id, new_stable_id),
    ).fetchone()
    return row is not None


def _cluster_for_member(conn: sqlite3.Connection, stable_id: str) -> int | None:
    row = conn.execute(
        "SELECT cluster_id FROM duplicate_clusters WHERE rationale = ? AND canonical_stable_id = ?",
        (_AUDIO_HASH_RATIONALE, stable_id),
    ).fetchone()
    if row is not None:
        return int(row[0])
    row = conn.execute(
        "SELECT a.cluster_id FROM track_aliases AS a "
        "JOIN duplicate_clusters AS c ON c.cluster_id = a.cluster_id "
        "WHERE c.rationale = ? AND a.alias_stable_id = ?",
        (_AUDIO_HASH_RATIONALE, stable_id),
    ).fetchone()
    return None if row is None else int(row[0])


def _canonical_for_cluster(conn: sqlite3.Connection, cluster_id: int, fallback: str) -> str:
    row = conn.execute(
        "SELECT canonical_stable_id FROM duplicate_clusters WHERE cluster_id = ?",
        (cluster_id,),
    ).fetchone()
    return fallback if row is None else str(row[0])


class HashDuplicateRecorder:
    """One ingest's writer into the dedup cluster DB.

    Satisfies ``apps.shared.state.ingest.folder.DuplicateRecorder`` structurally, so
    folder ingest records duplicates without importing this package.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def record(
        self,
        new_stable_id: str,
        existing_stable_id: str,
        audio_hash: str,
        *,
        new_path: str,
        existing_path: str,
    ) -> bool:
        return record_hash_duplicate(
            self._conn,
            new_stable_id,
            existing_stable_id,
            audio_hash,
            new_path=new_path,
            existing_path=existing_path,
        )

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


def open_hash_duplicate_recorder(db_path: Path) -> HashDuplicateRecorder:
    """Open (creating or migrating) the dedup DB at ``db_path`` for one ingest."""
    return HashDuplicateRecorder(ensure_schema(db_path))


__all__ = ["HashDuplicateRecorder", "open_hash_duplicate_recorder", "record_hash_duplicate"]
