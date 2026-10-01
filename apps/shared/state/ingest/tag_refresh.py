"""Re-read the file tags of rows whose import could not read them.

Packaged builds before Thu 1 Oct 2026 shipped without a tag reader (the GPL
mutagen was optional and absent), so every folder-imported row from them was
written with the filename as its title, no artist, no album and no duration.
A folder rescan never revisits a row it already has (``folder_rescan`` only
writes NEW paths), so those rows stayed blank forever even after the reader
came back: demon-llama's 100-track sample showed no artist on any row.

A row is a candidate when it has a file path and NO duration: a successful
read always yields a duration, so a NULL one is the signature of a read that
never happened. The refresh fills only what the failed read left empty and
never touches the file itself (Open DJ never writes into library files):

* ``title`` only while it is still the filename stem the import fell back to
* ``artists`` / ``album`` only while empty
* ``duration_ms`` only while NULL
* genre / comments / tag BPM / tag key through the import's own rule
  (:func:`apps.shared.state.ingest.folder.write_file_tag_metadata`)

Requirements (mini-PRD):
  ✔︎ blank rows get their tags back
    [if] a row reads title=stem, artists=[], duration NULL and its file is tagged [then] the tags land
    [if] a row was renamed by the user (title != stem) [then] its title is kept
    [if] a row already has a duration [then] it is not a candidate
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from apps.shared import audio_files
from apps.shared.state.ingest.folder import FolderIngestReport, write_file_tag_metadata
from apps.shared.state.writer import StateWriter


@dataclass(frozen=True)
class BlankRow:
    stable_id: str
    stable_id_tier: str
    file_path: str
    title: str | None
    artists: list[str]
    album: str | None
    isrc: str | None
    content_hash: str | None
    audio_hash: str | None


def blank_rows(conn: sqlite3.Connection, stable_ids: list[str] | None = None) -> list[BlankRow]:
    """Live rows with a file path and no duration, i.e. never tag-read."""
    sql = (
        "SELECT stable_id, stable_id_tier, file_path, title, artists_json, album, isrc, "
        "content_hash, audio_hash FROM tracks WHERE deleted_at IS NULL "
        "AND duration_ms IS NULL AND file_path IS NOT NULL AND file_path != ''"
    )
    params: list[str] = []
    if stable_ids is not None:
        if not stable_ids:
            return []
        sql += f" AND stable_id IN ({','.join('?' * len(stable_ids))})"
        params = list(stable_ids)
    return [
        BlankRow(
            stable_id=row[0], stable_id_tier=row[1], file_path=row[2], title=row[3],
            artists=list(json.loads(row[4] or "[]")), album=row[5], isrc=row[6],
            content_hash=row[7], audio_hash=row[8],
        )
        for row in conn.execute(sql, params).fetchall()
    ]


def refresh_row(writer: StateWriter, row: BlankRow) -> bool:
    """Fill ``row`` from its file's tags. False when the file still reads nothing."""
    metadata = audio_files.read_metadata(Path(row.file_path))
    if metadata is None:
        return False
    stem = Path(row.file_path).stem
    writer.upsert_track(
        stable_id=row.stable_id,
        stable_id_tier=row.stable_id_tier,
        title=metadata.title if metadata.title and row.title in (None, "", stem) else row.title,
        artists=row.artists or ([metadata.artist] if metadata.artist else []),
        album=row.album or metadata.album,
        isrc=row.isrc,
        duration_ms=int(metadata.duration_s * 1000) if metadata.duration_s else None,
        file_path=row.file_path,
        content_hash=row.content_hash,
        audio_hash=row.audio_hash,
    )
    write_file_tag_metadata(writer, row.stable_id, metadata, FolderIngestReport(roots=[]))
    return True


__all__ = ["BlankRow", "blank_rows", "refresh_row"]
