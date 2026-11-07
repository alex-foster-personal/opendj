"""Flat JSON and row mapping for set notes (SETNOTE-6340c)."""

from __future__ import annotations

import sqlite3

from .models import Geometry, SetNote


def row_to_note(row: sqlite3.Row | tuple) -> SetNote:
    scope, body_md, x, y, w, h, visible, rev, updated_at = row
    return SetNote(
        scope=scope,
        body_md=body_md,
        geometry=Geometry(x=x, y=y, w=w, h=h),
        visible=bool(visible),
        rev=int(rev),
        updated_at=updated_at,
    )


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


def geometry_from_optional(
    x: int | None,
    y: int | None,
    w: int | None,
    h: int | None,
) -> Geometry:
    return Geometry(x=x, y=y, w=w, h=h)


__all__ = ["geometry_from_optional", "note_to_wire", "row_to_note"]
