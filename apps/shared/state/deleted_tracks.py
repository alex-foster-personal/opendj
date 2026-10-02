"""Tracks the user removed: the read side of "deletes stay deleted" (LIBM-140).

Requirements (status: done + ran + regression tests):

1. An ingest can ask whether a file it is about to write is a track the user
   removed, by id or by content identity.
   - [if] the stable_id is tombstoned [then] the match is that id.
   - [if] another row with the same content_hash or audio_hash is tombstoned
     and no live row holds that identity [then] the match is the tombstone.
   - [if] nothing is tombstoned under that id or identity [then] None.
2. Removed tracks can be listed, newest removal first, so a person or an
   agent can see them and restore one (``StateWriter.undelete_track``).
   - [if] a track is removed [then] it is listed with its removal stamp.
   - [if] it is restored [then] it leaves the list.
   - [if] nothing was removed [then] the list is empty.

``tracks.deleted_at`` has two local writers, both through
``StateWriter.remove_from_library``, and ``tracks.deleted_reason`` tells them
apart: ``user`` is Remove from library, ``missing`` is a watched-folder rescan
that found the file gone. Only a ``user`` tombstone is deletion intent, and
only those match here; a ``missing`` one is lifted when the file is found
again. A tombstone with no reason predates the column and is read as ``user``.
"The vendor library no longer lists it" is neither: that is
``track_availability``, and it does not touch these columns.

The case-3 rule, a removed recording that reappears under a new path or id:
identity is the audio, not the path. A file whose ``content_hash`` or
``audio_hash`` equals a removed track's is the same recording and stays
removed on every bulk ingest. A file with different audio bytes (another
master, edit or encode) is a different recording and is imported. ISRC alone
is not used: an ISRC-tier ``stable_id`` already IS the ISRC, so those rows
meet their own tombstone by id, and two different files may share an ISRC.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from apps.shared.state.writer_tracks import DELETED_BY_USER, DELETED_FILE_MISSING, DeleteReason

# -----------------------------------------------------------------------------
# types
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class DeletedTrack:
    """One tombstoned track: removed by the user, or its file went missing."""

    stable_id: str
    title: str | None
    artists: list[str]
    file_path: str | None
    deleted_at: str
    reason: DeleteReason


def _is_user_tombstone(deleted_at: str | None, deleted_reason: str | None) -> bool:
    return deleted_at is not None and deleted_reason != DELETED_FILE_MISSING


# -----------------------------------------------------------------------------
# queries
# -----------------------------------------------------------------------------


def find_deleted_match(
    conn: sqlite3.Connection,
    *,
    stable_id: str,
    content_hash: str | None,
    audio_hash: str | None,
) -> str | None:
    """The removed track an incoming file IS, or None when it is not one.

    Only tombstones the USER wrote count (see the module docstring). One under
    the same ``stable_id`` always matches. One under another id matches on content identity only while no LIVE row carries that
    identity: once the user has restored the recording, or holds a second live
    copy of it, the identity is in the library on purpose and an ingest of it
    proceeds.
    """
    own = conn.execute("SELECT deleted_at, deleted_reason FROM tracks WHERE stable_id = ?", (stable_id,)).fetchone()
    if own is not None:
        return stable_id if _is_user_tombstone(own[0], own[1]) else None
    for column, digest in (("content_hash", content_hash), ("audio_hash", audio_hash)):
        if not digest:
            continue
        rows = conn.execute(
            f"SELECT stable_id, deleted_at, deleted_reason FROM tracks WHERE {column} = ? ORDER BY deleted_at DESC",
            (digest,),
        ).fetchall()
        if any(deleted_at is None for _, deleted_at, _reason in rows):
            continue
        removed = [sid for sid, deleted_at, reason in rows if _is_user_tombstone(deleted_at, reason)]
        if removed:
            return str(removed[0])
    return None


def list_deleted(conn: sqlite3.Connection) -> list[DeletedTrack]:
    """Every tombstoned track with why it is one, newest removal first."""
    rows = conn.execute(
        "SELECT stable_id, title, artists_json, file_path, deleted_at, deleted_reason "
        "FROM tracks WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC, stable_id"
    ).fetchall()
    return [
        DeletedTrack(
            stable_id=str(stable_id),
            title=title,
            artists=[str(artist) for artist in json.loads(artists_json or "[]")],
            file_path=file_path,
            deleted_at=str(deleted_at),
            reason=DELETED_FILE_MISSING if reason == DELETED_FILE_MISSING else DELETED_BY_USER,
        )
        for stable_id, title, artists_json, file_path, deleted_at, reason in rows
    ]


__all__ = ["DeletedTrack", "find_deleted_match", "list_deleted"]
