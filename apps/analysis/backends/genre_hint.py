"""The own beatgrid backfill's genre hint: the track's own tag, or nothing.

Split out of `own_beatgrid.py` to keep that module under the file-size ceiling.
The tag is the user's own genre, from state.db `track_fields.genre` or, for a
rekordbox-mapped track, its rekordbox genre field; never another program's BPM. It is handed to the octave
policy as a tempo-family hint (`apps.analysis_beatgrid.tempo_family`).

-Claude
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path

log = logging.getLogger("apps.analysis.backends.own_beatgrid")

#: Set to ``off`` to analyze without the library genre tag.
GENRE_HINT_ENV = "MDT_BEATGRID_GENRE_HINT"


def library_genre(stable_id: str, db_path: Path | None = None, master_path: Path | None = None) -> str | None:
    """The track's genre tag, or None when there is none to read.

    Two sources, both the user's own tag: state.db `track_fields.genre`
    (written by folder imports and by edits in the app), then, for a track
    mapped to rekordbox, its rekordbox genre (`djmdContent.GenreID`). The
    rekordbox ingest does not copy genre into `track_fields`, so without the
    second source every rekordbox-library track had no hint at all (measured
    on silver Fri 2 Oct 2026: 0 of 337 bench fixtures had a `track_fields`
    genre). `master_path` defaults to the plain rekordbox DB beside the state
    dir (`<data>/master.plain.db`).

    A hint, not an input the grid depends on: no state.db (a bench host, a
    bare checkout), no row, or ``MDT_BEATGRID_GENRE_HINT=off`` all mean "no
    hint", and the record's payload then carries no `tempo_family` block, so
    a record never claims a hint it did not use.
    """
    if os.environ.get(GENRE_HINT_ENV, "").strip().lower() == "off":
        return None
    from apps.shared.state import paths as state_paths
    from apps.shared.state.db import open_ro

    try:
        conn = open_ro(db_path)
    except (FileNotFoundError, sqlite3.OperationalError) as exc:
        log.info("no genre hint for %s: state.db unavailable (%s)", stable_id, exc)
        return None
    try:
        row = conn.execute(
            "SELECT value_json FROM track_fields WHERE stable_id = ? AND field_name = 'genre'",
            (stable_id,),
        ).fetchone()
        tag = _field_tag(row[0]) if row is not None else None
        vendor = None if tag is not None else _rekordbox_vendor_id(conn, stable_id)
    except sqlite3.OperationalError as exc:
        log.info("no genre hint for %s: %s", stable_id, exc)
        return None
    finally:
        conn.close()
    if tag is not None or vendor is None:
        return tag
    if master_path is None:
        master_path = (
            state_paths.REKORDBOX_PLAIN_DB if db_path is None else Path(db_path).parent.parent / "master.plain.db"
        )
    return _rekordbox_genre(stable_id, vendor, Path(master_path))


def _rekordbox_vendor_id(conn: sqlite3.Connection, stable_id: str) -> str | None:
    """The track's rekordbox content id, or None for an unmapped track or older schema."""
    try:
        row = conn.execute(
            "SELECT vendor_id FROM track_vendor_ids "
            "WHERE stable_id = ? AND vendor = 'rekordbox' AND deleted_at IS NULL",
            (stable_id,),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        log.info("no rekordbox mapping for %s: %s", stable_id, exc)
        return None
    return str(row[0]) if row is not None else None


def _field_tag(value_json: str) -> str | None:
    try:
        value = json.loads(value_json)
    except (TypeError, ValueError):
        value = value_json
    return (value.strip() or None) if isinstance(value, str) else None


def _rekordbox_genre(stable_id: str, vendor_id: str, master_path: Path) -> str | None:
    """The mapped track's rekordbox genre name, read-only, or None."""
    if not master_path.exists():
        log.info("no rekordbox genre for %s: %s not found", stable_id, master_path)
        return None
    try:
        rb = sqlite3.connect(f"{master_path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.OperationalError as exc:
        log.info("no rekordbox genre for %s: %s", stable_id, exc)
        return None
    try:
        row = rb.execute(
            "SELECT g.Name FROM djmdContent c "
            "JOIN djmdGenre g ON g.ID = c.GenreID AND g.rb_local_deleted = 0 "
            "WHERE c.ID = ? AND c.rb_local_deleted = 0",
            (vendor_id,),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        log.info("no rekordbox genre for %s: %s", stable_id, exc)
        return None
    finally:
        rb.close()
    name = row[0] if row is not None else None
    return (name.strip() or None) if isinstance(name, str) else None
