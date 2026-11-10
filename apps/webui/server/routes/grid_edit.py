"""Deck grid adjust / grid shift over HTTP (GRIDEDIT-6235..6235d).

The two left-edge deck buttons and any agent drive the SAME routes, so the UI
has no private path (AGENTS.md: a UI interaction is incomplete without HTTP,
IPC or CLI parity; the CLI twin is ``opendj track grid-edit``).

  GET    /api/v1/tracks/{stable_id}/grid-edit          the edit in force
  POST   /api/v1/tracks/{stable_id}/grid-edit/nudge    relative step (the clicks)
  PUT    /api/v1/tracks/{stable_id}/grid-edit          absolute value
  DELETE /api/v1/tracks/{stable_id}/grid-edit          reset to the measured grid
  GET    /api/v1/tracks/{stable_id}/grid-edit/anchors  the tempo anchors in force (GRIDEDIT-10220)
  POST   /api/v1/tracks/{stable_id}/grid-edit/anchors  {at_s, bpm}: add (or replace within half a beat)
  DELETE /api/v1/tracks/{stable_id}/grid-edit/anchors/{at_s}  remove the anchor near at_s
  POST   /api/v1/tracks/{stable_id}/grid-edit/undo     step back one edit (409 when none)
  PUT    /api/v1/tracks/{stable_id}/bpm-pick            {"source": "own"|"mik"}: settle a
                                                       disputed tempo (GRID-OCTAVE-02)
  DELETE /api/v1/tracks/{stable_id}/bpm-pick            forget the pick (the dispute shows again)

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
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from apps.adapters.rekordbox import config as rb_config
from apps.shared.events import publish
from apps.shared.state import db as state_db
from apps.shared.state import grid_edit, tempo_pick
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
MOUNT_UNDER_API_PREFIX = True

#: ``library.changed`` kind every write publishes (mirrored in events-bus.ts).
EVENT_KIND = "beatgrid_edit"


class AnchorOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    #: Served time (after the grid adjust) the tempo change starts at.
    at_s: float
    bpm: float


class GridEditOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    stable_id: str
    #: Which grid this edit applies to.
    source: grid_edit.GridSource
    #: Grid adjust: every beat moved by this many ms, later when positive.
    offset_ms: float
    #: Grid shift: bar 1 moved by this many whole beats, later when positive (-1..2).
    beat_shift: int
    #: Tempo anchors, sorted by time; empty when the grid keeps its measured tempo.
    anchors: list[AnchorOut] = Field(default_factory=list)
    #: Constant-tempo override; null with ``anchor_t`` when the measured grid stands.
    bpm: float | None = None
    #: Served-time anchor the override is tied to; null when ``bpm`` is null.
    anchor_t: float | None = None
    #: When the edit in force was made (an undo restores the earlier edit's time); null when never edited.
    modified_at: str | None


class GridEditNudgeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: grid_edit.GridSource | None = None
    #: Relative grid-adjust step in ms, later when positive.
    offset_ms: float = Field(0.0, ge=-grid_edit.MAX_OFFSET_MS, le=grid_edit.MAX_OFFSET_MS)
    #: Relative grid-shift step in whole beats, later when positive.
    beats: StrictInt = Field(0, ge=-64, le=64)
    #: Constant-tempo override; must be paired with ``anchor_t``.
    bpm: float | None = None
    anchor_t: float | None = None


class GridEditSetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: grid_edit.GridSource | None = None
    offset_ms: float
    beat_shift: StrictInt = Field(ge=-64, le=64)
    bpm: float | None = None
    anchor_t: float | None = None


class GridEditAnchorIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: grid_edit.GridSource | None = None
    #: Served time in seconds; the anchor snaps to the grid line nearest it.
    at_s: float
    #: Tempo from the anchor on (40..300, checked by the kernel).
    bpm: float


class BpmPickIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Whose tempo the deck and the library serve where ours and Mixed In Key's disagree.
    source: tempo_pick.TempoPick


class BpmPickOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    stable_id: str
    #: The stored pick; null once cleared.
    source: tempo_pick.TempoPick | None


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
        anchors=[AnchorOut(at_s=a.at_s, bpm=a.bpm) for a in edit.anchors],
        bpm=edit.bpm,
        anchor_t=edit.anchor_t,
        modified_at=stored.modified_at,
    )


_Plan = Callable[
    [sqlite3.Connection, grid_edit.GridSource, grid_edit.StoredGridEdits],
    tuple[Mapping[grid_edit.GridSource, grid_edit.GridEdit], str],
]


def _write_stored(request: Request, stable_id: str, source: grid_edit.GridSource | None, plan: _Plan) -> GridEditOut:
    """One read-modify-write of the track's edits under a writer unit.

    ``plan`` returns the edits to store and their ``modified_at``; a
    ``GridEditError`` it raises rolls back and publishes nothing.
    """
    resolved = source if source is not None else _served_source(request, stable_id)
    conn = state_db.open_rw(state_db_path(request))
    try:
        with StateWriter(conn, actor="webui") as writer, writer.transaction():
            _require_track(conn, stable_id)
            edits, modified_at = plan(conn, resolved, grid_edit.read(conn, stable_id))
            writer.set_field(
                stable_id,
                grid_edit.FIELD,
                grid_edit.blob(edits),
                source=grid_edit.OWN_SOURCE,
                modified_at=modified_at,
            )
            after = grid_edit.read(conn, stable_id)
    except grid_edit.GridEditError as exc:
        if conn.in_transaction:
            conn.rollback()
        _raise(exc)
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()
    publish("library.changed", {"kind": EVENT_KIND, "ids": [stable_id]})
    return _out(stable_id, resolved, after)


def _write(
    request: Request,
    stable_id: str,
    source: grid_edit.GridSource | None,
    change: Callable[[grid_edit.GridEdit], grid_edit.GridEdit],
) -> GridEditOut:
    """A new edit of one grid source, stamped now."""
    return _write_stored(
        request,
        stable_id,
        source,
        lambda _conn, resolved, stored: (
            grid_edit.with_edit(stored, resolved, change(stored.for_source(resolved))),
            canonical_now(),
        ),
    )


def _read_out(request: Request, stable_id: str, source: grid_edit.GridSource | None) -> GridEditOut:
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


@router.get("/{stable_id}/grid-edit", response_model=GridEditOut)
def get_grid_edit(
    request: Request,
    stable_id: str,
    source: grid_edit.GridSource | None = None,
    _backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> GridEditOut:
    return _read_out(request, stable_id, source)


@router.get("/{stable_id}/grid-edit/anchors", response_model=GridEditOut)
def get_grid_anchors(
    request: Request,
    stable_id: str,
    source: grid_edit.GridSource | None = None,
    _backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> GridEditOut:
    return _read_out(request, stable_id, source)


@router.post("/{stable_id}/grid-edit/anchors", response_model=GridEditOut)
def add_grid_anchor(
    request: Request,
    stable_id: str,
    body: GridEditAnchorIn,
    _backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> GridEditOut:
    """Change tempo at ``at_s``; an anchor within half a beat of an existing one replaces it."""
    return _write(request, stable_id, body.source, lambda edit: grid_edit.with_anchor(edit, body.at_s, body.bpm))


@router.delete("/{stable_id}/grid-edit/anchors/{at_s}", response_model=GridEditOut)
def remove_grid_anchor(
    request: Request,
    stable_id: str,
    at_s: float,
    source: grid_edit.GridSource | None = None,
    _backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> GridEditOut:
    return _write(request, stable_id, source, lambda edit: grid_edit.without_anchor(edit, at_s))


@router.post("/{stable_id}/grid-edit/nudge", response_model=GridEditOut)
def nudge_grid_edit(
    request: Request,
    stable_id: str,
    body: GridEditNudgeIn,
    _backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> GridEditOut:
    """Move the grid by a step: ``offset_ms`` is grid adjust, ``beats`` grid shift."""
    if body.offset_ms == 0 and body.beats == 0 and body.bpm is None and body.anchor_t is None:
        raise HTTPException(
            status_code=422,
            detail={"code": "GRID_EDIT_EMPTY", "message": "a nudge needs offset_ms, beats, or bpm"},
        )
    if (body.bpm is None) != (body.anchor_t is None):
        raise HTTPException(
            status_code=422,
            detail={"code": "GRID_EDIT_INVALID", "message": "bpm and anchor_t must be set together"},
        )
    if body.bpm is not None:
        return _write(
            request,
            stable_id,
            body.source,
            lambda edit: grid_edit.make_edit(
                edit.offset_ms,
                edit.beat_shift,
                anchors=edit.anchors,
                bpm=body.bpm,
                anchor_t=body.anchor_t,
            ),
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
        lambda edit: grid_edit.make_edit(
            body.offset_ms,
            body.beat_shift,
            anchors=edit.anchors,
            bpm=body.bpm,
            anchor_t=body.anchor_t,
        ),
    )


@router.delete("/{stable_id}/grid-edit", response_model=GridEditOut)
def reset_grid_edit(
    request: Request,
    stable_id: str,
    source: grid_edit.GridSource | None = None,
    part: Literal["adjust", "shift", "bpm"] | None = None,
    _backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> GridEditOut:
    """Back to the measured grid. A write of the identity, so history keeps the old edit.

    ``part`` resets one half and keeps the other, read and written in the same
    transaction, so an agent's concurrent edit of the kept half is never lost.
    """
    if part == "adjust":
        return _write(
            request,
            stable_id,
            source,
            lambda edit: grid_edit.make_edit(
                0.0, edit.beat_shift, anchors=edit.anchors, bpm=edit.bpm, anchor_t=edit.anchor_t
            ),
        )
    if part == "shift":
        return _write(
            request,
            stable_id,
            source,
            lambda edit: grid_edit.make_edit(
                edit.offset_ms, 0, anchors=edit.anchors, bpm=edit.bpm, anchor_t=edit.anchor_t
            ),
        )
    if part == "bpm":
        return _write(
            request,
            stable_id,
            source,
            lambda edit: grid_edit.make_edit(edit.offset_ms, edit.beat_shift, anchors=edit.anchors),
        )
    return _write(request, stable_id, source, lambda _edit: grid_edit.IDENTITY)


@router.post("/{stable_id}/grid-edit/undo", response_model=GridEditOut)
def undo_grid_edit(
    request: Request,
    stable_id: str,
    source: grid_edit.GridSource | None = None,
    _backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> GridEditOut:
    """Step back one stored edit (GRIDEDIT-10215); 409 ``GRID_EDIT_NOTHING_TO_UNDO`` when none.

    The undo writes the previous value back, so it is in history itself and the
    next undo steps back again. It covers the track's last grid write whichever
    grid source it touched; ``source`` only picks the grid the answer reports.
    """

    def plan(
        conn: sqlite3.Connection, _resolved: grid_edit.GridSource, _stored: grid_edit.StoredGridEdits
    ) -> tuple[Mapping[grid_edit.GridSource, grid_edit.GridEdit], str]:
        step = grid_edit.previous(conn, stable_id)
        if step is None:
            raise grid_edit.GridEditError(
                409, "GRID_EDIT_NOTHING_TO_UNDO", f"no earlier grid edit to undo for {stable_id}"
            )
        return step.edits, step.modified_at

    return _write_stored(request, stable_id, source, plan)


def _served_grid_source(stable_id: str, payload: dict[str, Any]) -> grid_edit.GridSource:
    source = (payload.get("beatgrid") or {}).get("source")
    if source not in grid_edit.GRID_SOURCES:
        raise RuntimeError(f"/anlz payload for {stable_id} names beatgrid source {source!r}")
    return source


def stored_grid_edit(request: Request, stable_id: str, payload: dict[str, Any]) -> grid_edit.GridEdit:
    """This track's stored edit for the grid ``payload`` serves; the identity when none."""
    source = _served_grid_source(stable_id, payload)
    stored = grid_edit.NO_EDITS
    conn = state_conn_ro(state_db_path(request))
    if conn is not None:
        try:
            if table_present(conn, "track_fields"):
                stored = grid_edit.read(conn, stable_id)
        finally:
            conn.close()
    return stored.for_source(source)


def apply_stored_grid_edit(request: Request, stable_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Apply this track's stored edit for the grid ``payload`` serves (``/anlz``)."""
    source = _served_grid_source(stable_id, payload)
    return grid_edit.apply_to_payload(payload, stored_grid_edit(request, stable_id, payload), source)


# ----------------------------------------------------------------- tempo pick (GRID-OCTAVE-02)


def stored_tempo_pick(request: Request, stable_id: str) -> tempo_pick.TempoPick | None:
    """The track's stored pick between our tempo and Mixed In Key's; None when none."""
    conn = state_conn_ro(state_db_path(request))
    if conn is None:
        return None
    try:
        return tempo_pick.read(conn, stable_id)
    finally:
        conn.close()


def _write_tempo_pick(request: Request, stable_id: str, pick: tempo_pick.TempoPick | None) -> BpmPickOut:
    conn = state_db.open_rw(state_db_path(request))
    try:
        with StateWriter(conn, actor="webui") as writer, writer.transaction():
            _require_track(conn, stable_id)
            writer.set_field(
                stable_id,
                tempo_pick.FIELD,
                tempo_pick.blob(pick),
                source=tempo_pick.OWN_SOURCE,
                modified_at=canonical_now(),
            )
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()
    # The grid /anlz serves changes (decks re-read on beatgrid_edit) and so does the row's BPM.
    publish("library.changed", {"kind": EVENT_KIND, "ids": [stable_id]})
    publish("library.changed", {"kind": "tracks", "ids": [stable_id]})
    return BpmPickOut(stable_id=stable_id, source=pick)


@router.put("/{stable_id}/bpm-pick", response_model=BpmPickOut)
def set_bpm_pick(
    request: Request,
    stable_id: str,
    body: BpmPickIn,
    _backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> BpmPickOut:
    """Serve ``source``'s tempo where ours and Mixed In Key's disagree; the dispute marker goes."""
    return _write_tempo_pick(request, stable_id, body.source)


@router.delete("/{stable_id}/bpm-pick", response_model=BpmPickOut)
def clear_bpm_pick(
    request: Request,
    stable_id: str,
    _backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> BpmPickOut:
    return _write_tempo_pick(request, stable_id, None)


__all__ = [
    "EVENT_KIND",
    "AnchorOut",
    "BpmPickIn",
    "BpmPickOut",
    "GridEditAnchorIn",
    "GridEditNudgeIn",
    "GridEditOut",
    "GridEditSetIn",
    "apply_stored_grid_edit",
    "router",
    "stored_grid_edit",
    "stored_tempo_pick",
]
