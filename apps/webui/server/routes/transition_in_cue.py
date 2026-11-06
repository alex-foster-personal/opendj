"""The transition-in cue: where the user expects an incoming transition to land (AGENT-51).

One cue per track, kind ``transition_in`` in Open DJ's own cue store
(``apps/shared/state/cue_store.py``, ADR-0128), mimicking Traktor's Fade In
cue and Mixxx's intro start. The AutoDJ planner reads it from ``/anlz`` and
makes it the incoming track's in window (``apps/autodj/windows.py``).

Contract:

  * ``GET    /api/v1/tracks/{stable_id}/transition-in`` -> ``{"cue": cue | null}``
  * ``PUT    /api/v1/tracks/{stable_id}/transition-in`` ``{"in_ms": n}`` -> the
    cue as stored, snapped to the nearest downbeat, plus ``requested_ms`` and ``bar``
  * ``DELETE /api/v1/tracks/{stable_id}/transition-in`` -> ``{"cue": null, "cleared": bool}``

The snap reads the downbeats from this app's own ``/anlz`` answer for the
track (same beatgrid-source selection, same overlays), so the stored point is
a bar of exactly the grid the planner will count on. A track with fewer than
two downbeats is refused with 422 ``NO_BEATGRID``: there is no bar to put the
cue on, and guessing one would hand the planner a window off the grid.

CLI twin: ``opendj track transition-in <stable_id> [set <ms>|clear]``.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, StrictInt

from apps.autodj.http import ProfileError, downbeats_of, snap_to_downbeat
from apps.shared import runtime_policy
from apps.shared.events import publish
from apps.shared.state import cue_store
from apps.shared.state import db as state_db
from apps.shared.state.sync_stamp import canonical_now
from apps.shared.state.writer import StateWriter

from ..backend import StateBackend
from ..deps import get_read_state, get_write_state
from . import rb_assets
from .rb_hot_cues import _duration_ms, _rekordbox_content, _state_db_path, _vendor_cues

router = APIRouter(prefix="/tracks", tags=["transition-in-cue"])


class TransitionInSetIn(BaseModel):
    in_ms: StrictInt = Field(
        ge=0, description="Where the transition should land, in ms. Snapped to the nearest downbeat."
    )


class TransitionInCueOut(BaseModel):
    kind: str
    in_ms: int


class TransitionInReadOut(BaseModel):
    cue: TransitionInCueOut | None


class TransitionInSetOut(BaseModel):
    cue: TransitionInCueOut
    requested_ms: int
    bar: int = Field(description="1-based bar on the track's downbeats, as the AutoDJ planner counts them")


class TransitionInClearOut(BaseModel):
    cue: None = None
    cleared: bool


def _cue_out(cue: dict[str, Any]) -> TransitionInCueOut:
    return TransitionInCueOut(kind=cue["kind"], in_ms=cue["in_ms"])


def _snap(request: Request, stable_id: str, in_ms: int, backend: StateBackend) -> tuple[int, int]:
    """``(snapped_ms, bar)`` on the downbeats this app's ``/anlz`` serves for the track."""
    response = rb_assets.get_track_anlz(request, stable_id, runtime_policy.ANLZ_POINTS_MIN, backend)
    if response.status_code != 200:
        raise HTTPException(
            502, detail={"code": "ANLZ_UNREADABLE", "message": f"/anlz answered {response.status_code}"}
        )
    anlz = json.loads(bytes(response.body))
    try:
        downbeats = downbeats_of(stable_id, anlz)
    except ProfileError as error:
        raise HTTPException(422, detail={"code": "NO_BEATGRID", "message": str(error)}) from error
    bar, seconds = snap_to_downbeat(downbeats, in_ms / 1000.0)
    return round(seconds * 1000.0), bar


def _write(request: Request, stable_id: str, op: Any) -> Any:
    """One read-modify-write of the track's cue set under a writer unit."""
    content = _rekordbox_content(stable_id)
    conn = state_db.open_rw(_state_db_path(request))
    try:
        with StateWriter(conn, actor="webui") as writer, writer.transaction():
                state = cue_store.load(conn, stable_id, _vendor_cues(content))

                def write(blob: dict[str, Any]) -> None:
                    writer.set_field(
                        stable_id, cue_store.CUE_FIELD, blob, source=cue_store.OWN_SOURCE, modified_at=canonical_now()
                    )

                result = op(state, _duration_ms(conn, stable_id, content), write)
    except cue_store.CueStoreError as exc:
        if conn.in_transaction:
            conn.rollback()
        raise HTTPException(status_code=exc.status, detail=exc.detail()) from exc
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()
    publish("library.changed", {"kind": "transition_in", "ids": [stable_id]})
    return result


@router.get("/{stable_id}/transition-in", response_model=TransitionInReadOut)
def read_transition_in(
    stable_id: str,
    request: Request,
    _backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> TransitionInReadOut:
    content = _rekordbox_content(stable_id)
    conn = state_db.open_ro(_state_db_path(request))
    try:
        state = cue_store.load(conn, stable_id, _vendor_cues(content))
    finally:
        conn.close()
    try:
        cue = cue_store.transition_in_cue(state)
    except cue_store.CueStoreError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail()) from exc
    return TransitionInReadOut(cue=None if cue is None else _cue_out(cue))


@router.put("/{stable_id}/transition-in", response_model=TransitionInSetOut)
def set_transition_in(
    stable_id: str,
    body: TransitionInSetIn,
    request: Request,
    backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> TransitionInSetOut:
    _rekordbox_content(stable_id)  # 404 before any grid read for an unknown track
    snapped_ms, bar = _snap(request, stable_id, body.in_ms, backend)
    cue = _write(
        request,
        stable_id,
        lambda state, duration_ms, write: cue_store.save_transition_in(
            state, snapped_ms, duration_ms=duration_ms, write=write
        ),
    )
    return TransitionInSetOut(cue=_cue_out(cue), requested_ms=body.in_ms, bar=bar)


@router.delete("/{stable_id}/transition-in", response_model=TransitionInClearOut)
def clear_transition_in(
    stable_id: str,
    request: Request,
    _backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> TransitionInClearOut:
    cleared = _write(
        request, stable_id, lambda state, _duration_ms, write: cue_store.clear_transition_in(state, write=write)
    )
    return TransitionInClearOut(cleared=cleared)


__all__ = ["router"]
