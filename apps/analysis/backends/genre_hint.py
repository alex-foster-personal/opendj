"""The own beatgrid backfill's genre hint: the track's own tag from state.db, or nothing.

Split out of `own_beatgrid.py` to keep that module under the file-size ceiling.
The tag is state.db `track_fields.genre`: the user's own genre, from the file
or their DJ library, never another program's BPM. It is handed to the octave
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


def library_genre(stable_id: str, db_path: Path | None = None) -> str | None:
    """The track's genre tag from state.db, or None when there is none to read.

    A hint, not an input the grid depends on: no state.db (a bench host, a
    bare checkout), no row, or ``MDT_BEATGRID_GENRE_HINT=off`` all mean "no
    hint", and the record's payload then carries no `tempo_family` block, so
    a record never claims a hint it did not use.
    """
    if os.environ.get(GENRE_HINT_ENV, "").strip().lower() == "off":
        return None
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
    except sqlite3.OperationalError as exc:
        log.info("no genre hint for %s: %s", stable_id, exc)
        return None
    finally:
        conn.close()
    if row is None:
        return None
    try:
        value = json.loads(row[0])
    except (TypeError, ValueError):
        value = row[0]
    return (value.strip() or None) if isinstance(value, str) else None
