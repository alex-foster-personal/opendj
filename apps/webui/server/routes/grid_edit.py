"""Deck grid adjust / grid shift over HTTP (GRIDEDIT-6235..6235d).

The two left-edge deck buttons and any agent drive the SAME routes, so the UI
has no private path (AGENTS.md: a UI interaction is incomplete without HTTP,
IPC or CLI parity; the CLI twin is ``opendj track grid-edit``).

  GET    /api/v1/tracks/{stable_id}/grid-edit          the edit in force
  POST   /api/v1/tracks/{stable_id}/grid-edit/nudge    relative step (the clicks)
  PUT    /api/v1/tracks/{stable_id}/grid-edit          absolute value
  DELETE /api/v1/tracks/{stable_id}/grid-edit          reset to the measured grid

``source`` picks which grid (rekordbox or own) the edit belongs to. Omitted,
it is the grid ``/anlz`` serves this track right now, resolved by the same
``beatgrid_source_for_track`` ``/anlz`` and ``/grid-provenance`` use, so the
route can never edit a grid other than the one the deck draws.

Nothing here writes to rekordbox: the edit is Open DJ's own ``track_fields``
row (``apps/shared/state/grid_edit.py``), applied on the way out of ``/anlz``
by :func:`apply_stored_grid_edit`. Every write publishes ``library.changed``
kind ``beatgrid_edit`` so an open deck re-reads its grid after an agent edit.

-Claude
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from apps.adapters.rekordbox import config as rb_config
from apps.shared.events import publish
from apps.shared.state import db as state_db
from apps.shared.state import grid_edit
from apps.shared.state.sync_stamp import canonical_now
from apps.shared.state.writer import StateWriter

from .. import rb_vendor
from ..backend import StateBackend
from ..deps import get_read_state, get_write_state
from ..rb_vendor_pkg.anlz_missing_analysis import (
    own_beatgrid_served_when_vendor_absent,
    vendor_analysis_path_absent,
)
from ..rb_vendor_pkg.own_lane_store import state_conn_ro, table_present
from .rb_assets_beatgrid_source import beatgrid_source_for_track

router = APIRouter(prefix="/tracks", tags=["grid-edit"])

#: ``library.changed`` kind every write publishes (mirrored in events-bus.ts).
EVENT_KIND = "beatgrid_edit"


class GridEditOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    stable_id: str
    #: Which grid this edit applies to.
    source: grid_edit.GridSource
    #: Grid adjust: every beat moved by this many ms, later when positive.
    offset_ms: float
    #: Grid shift: bar 1 moved by this many whole beats, later when positive (-1..2).
    beat_shift: int
    #: When the stored row was last written (any source); null when never edited.
    modified_at: str | None


class GridEditNudgeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: grid_edit.GridSource | None = None
    #: Relative grid-adjust step in ms, later when positive.
    offset_ms: float = Field(0.0, ge=-grid_edit.MAX_OFFSET_MS, le=grid_edit.MAX_OFFSET_MS)
    #: Relative grid-shift step in whole beats, later when positive.
    beats: StrictInt = Field(0, ge=-64, le=64)


class GridEditSetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: grid_edit.GridSource | None = None
    offset_ms: float
    beat_shift: StrictInt = Field(ge=-64, le=64)


def _raise(exc: grid_edit.GridEditError) -> NoReturn:
    raise HTTPException(status_code=exc.status, detail=exc.detail()) from exc


def state_db_path(request: Request) -> Path:
    """The state DB both the writes here and the ``/anlz`` overlay use (same rule as hot cues)."""
    path = getattr(request.app.state, "state_db_path", None)
    return Path(path) if path is not None else rb_config.STATE_DB


def _served_source(request: Request, stable_id: str) -> grid_edit.GridSource:
    """The grid ``/anlz`` serves this track now, by the same rules as that route."""
    has_rb_mapping = stable_id in rb_vendor.bulk_rb_meta([stable_id])
    source, _basis = beatgrid_source_for_track(request, has_rb_mapping=has_rb_mapping)
    if source not in grid_edit.GRID_SOURCES:
        raise RuntimeError(f"unhandled beatgrid source {source!r}")
    if source == "rekordbox" and has_rb_mapping and _own_served_for_vendor_absent(request, stable_id):
        return "own"
    return source  # type: ignore[return-value]  # narrowed by the check above


def _own_served_for_vendor_absent(request: Request, stable_id: str) -> bool:
    """``/anlz``'s issue #2346 rescue: a mapped track rekordbox never analyzed
    serves its own grid even while the lane selection says rekordbox."""
    try:
        content = rb_vendor.resolve_content(stable_id)
    except HTTPException as exc:
        # Only the miss /anlz itself serves through (its local branch) means
        # "no rescue"; any other resolution failure fails the edit the same way
        # it fails /anlz, rather than guessing which grid the deck draws.
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        if detail.get("code") == "VENDOR_MAPPING_NOT_FOUND":
            return False
        raise
    if not vendor_analysis_path_absent(content):
        return False
    # Same database the /anlz rescue reads (rb_assets._state_db_override).
    override = getattr(request.app.state, "analysis_db_path", None)
    return own_beatgrid_served_when_vendor_absent(stable_id, Path(override) if override is not None else None)


def _require_track(conn: sqlite3.Connection, stable_id: str) -> None:
    row = conn.execute("SELECT 1 FROM tracks WHERE stable_id = ? AND deleted_at IS NULL", (stable_id,)).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "TRACK_NOT_FOUND", "message": f"no track {stable_id}"},
        )


def _out(stable_id: str, source: grid_edit.GridSource, stored: grid_edit.StoredGridEdits) -> GridEditOut:
    edit = stored.for_source(source)
    return GridEditOut(
        stable_id=stable_id,
        source=source,
        offset_ms=edit.offset_ms,
        beat_shift=edit.beat_shift,
        modified_at=stored.modified_at,
    )


def _write(
    request: Request,
    stable_id: str,
    source: grid_edit.GridSource | None,
    change: Callable[[grid_edit.GridEdit], grid_edit.GridEdit],
) -> GridEditOut:
    """One read-modify-write of the track's edit under BEGIN IMMEDIATE."""
    resolved = source if source is not None else _served_source(request, stable_id)
    conn = state_db.open_rw(state_db_path(request))
    try:
        conn.execute("BEGIN IMMEDIATE")
        _require_track(conn, stable_id)
        stored = grid_edit.read(conn, stable_id)
        edits = grid_edit.with_edit(stored, resolved, change(stored.for_source(resolved)))
        with StateWriter(conn, actor="webui") as writer:
            writer.set_field(
                stable_id,
                grid_edit.FIELD,
                grid_edit.blob(edits),
                source=grid_edit.OWN_SOURCE,
                modified_at=canonical_now(),
            )
        after = grid_edit.read(conn, stable_id)
        conn.commit()
    except grid_edit.GridEditError as exc:
        conn.rollback()
        _raise(exc)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    publish("library.changed", {"kind": EVENT_KIND, "ids": [stable_id]})
    return _out(stable_id, resolved, after)


@router.get("/{stable_id}/grid-edit", response_model=GridEditOut)
def get_grid_edit(
    request: Request,
    stable_id: str,
    source: grid_edit.GridSource | None = None,
    _backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> GridEditOut:
    resolved = source if source is not None else _served_source(request, stable_id)
    conn = state_conn_ro(state_db_path(request))
    if conn is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "STATE_DB_UNAVAILABLE", "message": "no state database to read"},
        )
    try:
        _require_track(conn, stable_id)
        stored = grid_edit.read(conn, stable_id)
    finally:
        conn.close()
    return _out(stable_id, resolved, stored)


@router.post("/{stable_id}/grid-edit/nudge", response_model=GridEditOut)
def nudge_grid_edit(
    request: Request,
    stable_id: str,
    body: GridEditNudgeIn,
    _backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> GridEditOut:
    """Move the grid by a step: ``offset_ms`` is grid adjust, ``beats`` grid shift."""
    if body.offset_ms == 0 and body.beats == 0:
        raise HTTPException(
            status_code=422,
            detail={"code": "GRID_EDIT_EMPTY", "message": "a nudge needs offset_ms or beats"},
        )
    return _write(
        request,
        stable_id,
        body.source,
        lambda edit: grid_edit.nudged(edit, offset_ms=body.offset_ms, beats=body.beats),
    )


@router.put("/{stable_id}/grid-edit", response_model=GridEditOut)
def set_grid_edit(
    request: Request,
    stable_id: str,
    body: GridEditSetIn,
    _backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> GridEditOut:
    return _write(
        request,
        stable_id,
        body.source,
        lambda _edit: grid_edit.make_edit(body.offset_ms, body.beat_shift),
    )


@router.delete("/{stable_id}/grid-edit", response_model=GridEditOut)
def reset_grid_edit(
    request: Request,
    stable_id: str,
    source: grid_edit.GridSource | None = None,
    part: Literal["adjust", "shift"] | None = None,
    _backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> GridEditOut:
    """Back to the measured grid. A write of the identity, so history keeps the old edit.

    ``part`` resets one half and keeps the other, read and written in the same
    transaction, so an agent's concurrent edit of the kept half is never lost.
    """
    if part == "adjust":
        return _write(request, stable_id, source, lambda edit: grid_edit.make_edit(0.0, edit.beat_shift))
    if part == "shift":
        return _write(request, stable_id, source, lambda edit: grid_edit.make_edit(edit.offset_ms, 0))
    return _write(request, stable_id, source, lambda _edit: grid_edit.IDENTITY)


def apply_stored_grid_edit(request: Request, stable_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Apply this track's stored edit for the grid ``payload`` serves (``/anlz``)."""
    source = (payload.get("beatgrid") or {}).get("source")
    if source not in grid_edit.GRID_SOURCES:
        raise RuntimeError(f"/anlz payload for {stable_id} names beatgrid source {source!r}")
    stored = grid_edit.NO_EDITS
    conn = state_conn_ro(state_db_path(request))
    if conn is not None:
        try:
            if table_present(conn, "track_fields"):
                stored = grid_edit.read(conn, stable_id)
        finally:
            conn.close()
    return grid_edit.apply_to_payload(payload, stored.for_source(source), source)


__all__ = [
    "EVENT_KIND",
    "GridEditNudgeIn",
    "GridEditOut",
    "GridEditSetIn",
    "apply_stored_grid_edit",
    "router",
]
