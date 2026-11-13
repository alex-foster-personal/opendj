"""Load and save set notes (SETNOTE-6340c).

A note is keyed by a scope string, not a foreign key, so scratch notes and
notes for playlists that live outside state.db share one table.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime

from .connection import open_set_notes
from .models import Geometry, SetNote
from .revision import StaleRevision, current_rev, next_rev_for_base
from .schema import apply_set_notes_migrations
from .wire import note_to_wire, row_to_note

_SCOPE_RE = re.compile(r"^(scratch|playlist:[^\s:]+|playlist_set:\d+)$")

_SET_NOTE_COLUMNS = "scope, body_md, x, y, w, h, visible, rev, updated_at"


def _ensure_migrated(conn: sqlite3.Connection) -> None:
    apply_set_notes_migrations(conn)


def _write_bind_values(
    body_md: str,
    geometry: Geometry,
    visible: bool,
    new_rev: int,
    updated_at: str,
    scope: str,
) -> tuple[object, ...]:
    return (
        body_md,
        geometry.x,
        geometry.y,
        geometry.w,
        geometry.h,
        int(visible),
        new_rev,
        updated_at,
        scope,
    )


__all__ = [
    "Geometry",
    "SetNote",
    "StaleRevision",
    "load_set_note",
    "list_set_notes",
    "note_to_wire",
    "open_set_notes",
    "save_set_note",
    "validate_scope",
]


def validate_scope(scope: str) -> str:
    if not _SCOPE_RE.fullmatch(scope):
        raise ValueError("scope must be 'scratch', 'playlist:<id>' or 'playlist_set:<int>'")
    return scope


def list_set_notes(conn: sqlite3.Connection) -> list[SetNote]:
    _ensure_migrated(conn)
    rows = conn.execute(f"SELECT {_SET_NOTE_COLUMNS} FROM set_notes ORDER BY scope").fetchall()
    return [row_to_note(row) for row in rows]


def load_set_note(conn: sqlite3.Connection, scope: str) -> SetNote:
    validate_scope(scope)
    _ensure_migrated(conn)
    row = conn.execute(
        f"SELECT {_SET_NOTE_COLUMNS} FROM set_notes WHERE scope = ?",
        (scope,),
    ).fetchone()
    if row is None:
        raise LookupError(f"no set note for scope {scope!r}")
    return row_to_note(row)


def save_set_note(
    conn: sqlite3.Connection,
    scope: str,
    body_md: str,
    geometry: Geometry,
    visible: bool,
    base_rev: int,
) -> SetNote:
    """Write a note; ``base_rev`` 0 creates, otherwise it must match the stored rev."""
    validate_scope(scope)
    _ensure_migrated(conn)
    stored = current_rev(conn, scope)
    new_rev = next_rev_for_base(base_rev, stored)
    updated_at = datetime.now(UTC).isoformat()
    params = _write_bind_values(body_md, geometry, visible, new_rev, updated_at, scope)
    if stored == 0:
        try:
            conn.execute(
                "INSERT INTO set_notes"
                "(body_md, x, y, w, h, visible, rev, updated_at, scope) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                params,
            )
        except sqlite3.IntegrityError:
            raise StaleRevision(current_rev(conn, scope)) from None
    else:
        cur = conn.execute(
            "UPDATE set_notes SET body_md = ?, x = ?, y = ?, w = ?, h = ?, "
            "visible = ?, rev = ?, updated_at = ? WHERE scope = ? AND rev = ?",
            (*params, stored),
        )
        if cur.rowcount != 1:
            raise StaleRevision(current_rev(conn, scope))
    return SetNote(
        scope=scope,
        body_md=body_md,
        geometry=replace(geometry),
        visible=visible,
        rev=new_rev,
        updated_at=updated_at,
    )
