"""Hidden-track redaction for public tracklists (now-playing feed, overlay).

A track flagged ``tracklist_hidden`` (JSON ``true`` in ``track_fields``) shows as
``ID - ID`` wherever a tracklist leaves the app, the DJ convention for an
unreleased or secret track. Issue #10337 owns the write path for the flag and
must reuse :func:`hidden_stable_ids`; if it stores the flag elsewhere, change
this one function.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence

TRACKLIST_HIDDEN_FIELD = "tracklist_hidden"
HIDDEN_LABEL = "ID"

_SQL_CHUNK = 500


def hidden_stable_ids(conn: sqlite3.Connection, stable_ids: Sequence[str]) -> set[str]:
    """The subset of ``stable_ids`` whose live ``tracklist_hidden`` field is JSON ``true``."""
    ids = list(dict.fromkeys(stable_ids))
    hidden: set[str] = set()
    for i in range(0, len(ids), _SQL_CHUNK):
        chunk = ids[i : i + _SQL_CHUNK]
        marks = ",".join("?" * len(chunk))
        for sid, value_json in conn.execute(
            f"SELECT stable_id, value_json FROM track_fields WHERE field_name = ? "  # placeholders only
            f"AND deleted_at IS NULL AND stable_id IN ({marks})",
            (TRACKLIST_HIDDEN_FIELD, *chunk),
        ):
            if json.loads(value_json) is True:
                hidden.add(str(sid))
    return hidden


__all__ = ["HIDDEN_LABEL", "TRACKLIST_HIDDEN_FIELD", "hidden_stable_ids"]
