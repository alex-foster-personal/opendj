"""The rekordbox genre of each track, for the ``library-tag`` provider (GENRE-04, GENRE-05).

The Library row reads the same value through ``bulk_rb_meta`` in the web layer;
this is the domain-side read, so the HTTP, CLI, MCP and genre-lane paths that
choose (and write) a genre see the same ``library-tag`` the row shows. Same
predicates: a live ``track_vendor_ids`` mapping and a live ``djmdContent`` row.

-Claude
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from apps.shared.state import db as state_db

_CHUNK = 500


def rekordbox_genres(conn: sqlite3.Connection, stable_ids: Sequence[str], master_path: Path) -> dict[str, str]:
    """stable_id -> rekordbox genre name, for mapped tracks that have one.

    No mapping means no rekordbox genre (an empty result is the true state). A
    mapping with no master database is an error, never "no genre".
    """
    ids = list(dict.fromkeys(stable_ids))
    vendor_by_sid: dict[str, str] = {}
    for start in range(0, len(ids), _CHUNK):
        chunk = ids[start : start + _CHUNK]
        marks = ",".join("?" * len(chunk))
        for sid, vid in conn.execute(
            "SELECT stable_id, vendor_id FROM track_vendor_ids "
            f"WHERE vendor = 'rekordbox' AND deleted_at IS NULL AND stable_id IN ({marks})",
            chunk,
        ):
            vendor_by_sid[str(sid)] = str(vid)
    if not vendor_by_sid:
        return {}
    if not master_path.is_file():
        raise FileNotFoundError(f"{len(vendor_by_sid)} tracks are mapped to rekordbox but {master_path} is missing")
    master = state_db.open_ro(master_path)
    try:
        genre_by_vid: dict[str, str] = {}
        vids = sorted(set(vendor_by_sid.values()))
        for start in range(0, len(vids), _CHUNK):
            chunk = vids[start : start + _CHUNK]
            marks = ",".join("?" * len(chunk))
            for vid, genre in master.execute(
                "SELECT c.ID, g.Name FROM djmdContent c "
                "LEFT JOIN djmdGenre g ON g.ID = c.GenreID AND g.rb_local_deleted = 0 "
                f"WHERE c.ID IN ({marks}) AND +c.rb_local_deleted = 0",
                chunk,
            ):
                if genre and str(genre).strip():
                    genre_by_vid[str(vid)] = str(genre).strip()
    finally:
        master.close()
    return {sid: genre_by_vid[vid] for sid, vid in vendor_by_sid.items() if vid in genre_by_vid}


def genre_text(value_json: str | None) -> str | None:
    """A ``track_fields`` genre value as non-empty text, else None.

    Values are JSON strings; a value that is not JSON is read as the text
    itself. One parser for every reader of the field in this package.
    """
    try:
        value = json.loads(value_json) if value_json else None
    except ValueError:
        value = value_json
    return value.strip() if isinstance(value, str) and value.strip() else None


def field_genres(conn: sqlite3.Connection, stable_ids: Sequence[str]) -> dict[str, tuple[str | None, str | None]]:
    """stable_id -> (user genre typed in the library, imported tag genre) from ``track_fields``."""
    out: dict[str, tuple[str | None, str | None]] = {}
    for start in range(0, len(stable_ids), _CHUNK):
        chunk = list(stable_ids[start : start + _CHUNK])
        marks = ",".join("?" * len(chunk))
        for sid, value_json, source in conn.execute(
            f"SELECT stable_id, value_json, source FROM track_fields "
            f"WHERE field_name = 'genre' AND deleted_at IS NULL AND stable_id IN ({marks})",
            chunk,
        ):
            text = genre_text(value_json)
            user, library = out.get(sid, (None, None))
            out[sid] = (text, library) if source == "webui" else (user, text)
    return out


__all__ = ["field_genres", "genre_text", "rekordbox_genres"]
