"""Load and save set notes (SETNOTE-6340c).

A note is keyed by a scope string, not a foreign key, so scratch notes and
notes for playlists that live outside state.db share one table.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from apps.shared.state import db as state_db

from .schema import apply_set_notes_migrations

_SCOPE_RE = re.compile(r"^(scratch|playlist:[^\s:]+|playlist_set:\d+)$")

_SET_NOTE_COLUMNS = "scope, body_md, x, y, w, h, visible, rev, updated_at"


@dataclass(slots=True)
class Geometry:
    x: int | None = None
    y: int | None = None
    w: int | None = None
    h: int | None = None


@dataclass(slots=True)
class SetNote:
    scope: str
    body_md: str
    geometry: Geometry
    visible: bool
    rev: int
    updated_at: str


def note_to_wire(note: SetNote) -> dict[str, object]:
    """The flat JSON shape the HTTP API and the CLI both emit."""
    g = note.geometry
    return {
        "scope": note.scope,
        "body_md": note.body_md,
        "x": g.x,
        "y": g.y,
        "w": g.w,
        "h": g.h,
        "visible": note.visible,
        "rev": note.rev,
        "updated_at": note.updated_at,
    }


class StaleRevision(Exception):
    def __init__(self, current_rev: int) -> None:
        super().__init__(f"stale base_rev; current rev is {current_rev}")
        self.current_rev = current_rev


def open_set_notes(db_path: Path) -> sqlite3.Connection:
    """Open an existing state.db read-write with the set-notes tables migrated."""
    if not db_path.exists():
        raise FileNotFoundError(f"missing {db_path}")
    conn = state_db.open_rw(db_path)
    apply_set_notes_migrations(conn)
    return conn


def validate_scope(scope: str) -> str:
    if not _SCOPE_RE.fullmatch(scope):
        raise ValueError(
            "scope must be 'scratch', 'playlist:<id>' or 'playlist_set:<int>'"
        )
    return scope


def _row_to_note(row: sqlite3.Row | tuple) -> SetNote:
    scope, body_md, x, y, w, h, visible, rev, updated_at = row
    return SetNote(
        scope=scope,
        body_md=body_md,
        geometry=Geometry(x=x, y=y, w=w, h=h),
        visible=bool(visible),
        rev=int(rev),
        updated_at=updated_at,
    )


def _current_rev(conn: sqlite3.Connection, scope: str) -> int:
    row = conn.execute(
        "SELECT rev FROM set_notes WHERE scope = ?", (scope,)
    ).fetchone()
    return int(row[0]) if row is not None else 0


def list_set_notes(conn: sqlite3.Connection) -> list[SetNote]:
    apply_set_notes_migrations(conn)
    rows = conn.execute(
        f"SELECT {_SET_NOTE_COLUMNS} FROM set_notes ORDER BY scope"
    ).fetchall()
    return [_row_to_note(row) for row in rows]


def load_set_note(conn: sqlite3.Connection, scope: str) -> SetNote:
    validate_scope(scope)
    apply_set_notes_migrations(conn)
    row = conn.execute(
        f"SELECT {_SET_NOTE_COLUMNS} FROM set_notes WHERE scope = ?",
        (scope,),
    ).fetchone()
    if row is None:
        raise LookupError(f"no set note for scope {scope!r}")
    return _row_to_note(row)


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
    apply_set_notes_migrations(conn)
    current = _current_rev(conn, scope)
    if base_rev != current:
        raise StaleRevision(current)
    new_rev = current + 1
    params = (
        body_md,
        geometry.x,
        geometry.y,
        geometry.w,
        geometry.h,
        int(visible),
        new_rev,
        datetime.now(UTC).isoformat(),
        scope,
    )
    if current == 0:
        try:
            conn.execute(
                "INSERT INTO set_notes"
                "(body_md, x, y, w, h, visible, rev, updated_at, scope) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                params,
            )
        except sqlite3.IntegrityError:
            raise StaleRevision(_current_rev(conn, scope)) from None
    else:
        cur = conn.execute(
            "UPDATE set_notes SET body_md = ?, x = ?, y = ?, w = ?, h = ?, "
            "visible = ?, rev = ?, updated_at = ? WHERE scope = ? AND rev = ?",
            (*params, current),
        )
        if cur.rowcount != 1:
            raise StaleRevision(_current_rev(conn, scope))
    return load_set_note(conn, scope)
