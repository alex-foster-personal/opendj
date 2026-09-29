"""SQLite compatibility queries for CloudSync track identity matching."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from apps.shared.state.ids import normalise_isrc
from apps.sync_hub import protocol


@dataclass(frozen=True)
class StoredMatch:
    """Stored track identity signals used by the identity resolver."""

    pk: str
    content_hash: str | None
    audio_hash: str | None
    isrc: str | None
    sort_key: tuple[str, str]


def _as_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _match_from_row(row: Sequence[Any]) -> StoredMatch:
    return StoredMatch(
        pk=str(row[0]),
        content_hash=_as_text(row[1]),
        audio_hash=_as_text(row[2]),
        isrc=_as_text(row[3]),
        sort_key=protocol.lww_key({protocol.UPDATED_AT: row[4], protocol.ORIGIN_DEVICE_ID: row[5]}),
    )


def matches_by_hash(
    conn: sqlite3.Connection,
    incoming_pk: str,
    content_hash: str | None,
    audio_hash: str | None,
) -> list[StoredMatch]:
    """Find rows sharing either supported content identity hash."""
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(tracks)")}
    if "audio_hash" not in columns:
        rows = conn.execute(
            "SELECT stable_id, content_hash, isrc, updated_at, origin_device_id "
            "FROM tracks WHERE content_hash = ? AND stable_id != ? "
            "AND deleted_at IS NULL",
            (content_hash, incoming_pk),
        ).fetchall()
        return [
            StoredMatch(
                pk=str(row[0]),
                content_hash=_as_text(row[1]),
                audio_hash=None,
                isrc=_as_text(row[2]),
                sort_key=protocol.lww_key(
                    {
                        protocol.UPDATED_AT: row[3],
                        protocol.ORIGIN_DEVICE_ID: row[4],
                    }
                ),
            )
            for row in rows
        ]
    # Each OR arm is served by its own index (idx_tracks_content_hash,
    # idx_tracks_audio_hash), so this is a seek, not a scan (#4397). That
    # plan returns rows arm by arm; ORDER BY rowid keeps the order the old
    # table scan returned.
    rows = conn.execute(
        """
        SELECT stable_id, content_hash, audio_hash, isrc, updated_at, origin_device_id
        FROM tracks
        WHERE stable_id != ?
          AND deleted_at IS NULL
          AND ((content_hash = ? AND ? IS NOT NULL)
            OR (audio_hash = ? AND ? IS NOT NULL))
        ORDER BY rowid
        """,
        (incoming_pk, content_hash, content_hash, audio_hash, audio_hash),
    ).fetchall()
    return [_match_from_row(row) for row in rows]


def matches_by_isrc(
    conn: sqlite3.Connection, incoming_pk: str, isrc: str, raw_isrc: str | None
) -> list[StoredMatch]:
    """Find rows sharing a normalizable ISRC across schema versions.

    Each OR arm has its own index (idx_tracks_isrc, idx_tracks_isrc_upper), so
    the lookup is a seek, not a scan (#4397).
    """
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(tracks)")}
    audio_column = "audio_hash" if "audio_hash" in columns else "NULL AS audio_hash"
    rows = conn.execute(
        f"""
        SELECT stable_id, content_hash, {audio_column}, isrc, updated_at, origin_device_id
        FROM tracks
        WHERE stable_id != ?
          AND deleted_at IS NULL
          AND isrc IS NOT NULL
          AND (isrc = ? OR upper(isrc) = ?)
        ORDER BY rowid
        """,
        (incoming_pk, raw_isrc or isrc, isrc),
    ).fetchall()
    return [
        match
        for match in (_match_from_row(row) for row in rows)
        if normalise_isrc(_as_text(match.isrc)) == isrc
    ]
