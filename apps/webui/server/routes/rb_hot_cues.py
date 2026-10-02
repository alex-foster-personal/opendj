"""Hot-cue SAVE/CLEAR/RESTORE endpoints over Open DJ's own cue store (CUES-01).

Cues live in ``state.db`` (``apps/shared/state/cue_store.py``), not in
rekordbox's ``djmdCue``, so every track can keep cues, rekordbox-mapped or
not. A track whose cues were never copied in falls back to its rekordbox cues
for reads, and its first edit seeds the own set from them. Nothing here writes
to rekordbox; write-back is the separate write-back-rekordbox-djay node.

Contract:

  * ``GET    /api/v1/tracks/{stable_id}/hot-cues`` -> eight slot revisions
  * ``PUT    /api/v1/tracks/{stable_id}/hot-cues/{slot}`` -> mutation + reversal token
  * ``DELETE /api/v1/tracks/{stable_id}/hot-cues/{slot}`` -> mutation + reversal token
  * ``PUT    /api/v1/tracks/{stable_id}/hot-cues/{slot}/restore`` -> CAS restore

``slot`` is a ``Literal['A'..'H']`` at the route level, so an unverified
Kind 9-11 slot is unreachable via the API rather than silently guessed at.
Every mutation requires the slot ETag through ``If-Match``. A stale caller
gets 409 plus the current ETag, with no partial mutation. SAVE and CLEAR
return a server-authoritative, single-use reversal token for restore.

The integrator wires ``router`` into ``create_app()`` under ``/api/v1``,
alongside the existing rb_assets router (same ``/tracks`` prefix)::

    from .routes import rb_hot_cues as rb_hot_cues_routes
    app.include_router(rb_hot_cues_routes.router, prefix=api_prefix)
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from apps.adapters.rekordbox import config as rb_config
from apps.shared.events import publish
from apps.shared.state import cue_store
from apps.shared.state import db as state_db
from apps.shared.state.sync_stamp import canonical_now
from apps.shared.state.writer import StateWriter

from .. import rb_vendor
from ..backend import StateBackend
from ..deps import get_write_state

router = APIRouter(prefix="/tracks", tags=["rb-hot-cues"])

HotCueSlot = Literal["A", "B", "C", "D", "E", "F", "G", "H"]

_IF_MATCH_OPENAPI_PARAMETER: dict[str, Any] = {
    "name": "If-Match",
    "in": "header",
    "required": True,
    "description": "Current slot revision returned by GET /tracks/{stable_id}/hot-cues",
    "schema": {"type": "string"},
}
_MUTATION_RESPONSES: dict[int, dict[str, Any]] = {
    409: {"description": "Slot revision, reversal scope, or reversal state is stale"},
    422: {"description": "Invalid cue position or restore token body"},
    428: {"description": "If-Match is required for every hot-cue mutation"},
}


class HotCueSaveIn(BaseModel):
    in_ms: StrictInt = Field(
        ge=0,
        description="Cue position in ms. Already quantized to the beatgrid "
                    "by the caller when quantize is enabled -- this "
                    "endpoint never guesses at beat alignment.",
    )
    comment: str | None = None
    color_table_index: int | None = Field(None, ge=0)


class AnlzCueOut(BaseModel):
    """Same shape as one COMPONENT-MAP 2.3 cue row (rb_vendor.fetch_cues)."""

    kind: Literal["hot_cue"]
    slot: HotCueSlot
    in_ms: int
    out_ms: int | None
    is_loop: bool
    active_loop: bool
    beat_loop_size: int | None
    color_table_index: int | None
    comment: str | None
    revision: str


class HotCueReversalOut(BaseModel):
    reversal_id: str


class HotCueMutationOut(BaseModel):
    cue: AnlzCueOut | None
    revision: str
    reversal: HotCueReversalOut | None = None


class HotCueSlotOut(BaseModel):
    slot: HotCueSlot
    cue: AnlzCueOut | None
    revision: str


class HotCueRestoreIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reversal_id: str = Field(min_length=1)


def _require_if_match(request: Request) -> str:
    if_match = request.headers.get("If-Match")
    if if_match:
        return if_match
    raise HTTPException(
        status_code=428,
        detail={
            "code": "HOT_CUE_REVISION_REQUIRED",
            "message": "hot-cue mutation requires an If-Match revision",
        },
    )


def _mutation_response(result: dict[str, Any], response: Response) -> HotCueMutationOut:
    revision = str(result.get("revision") or result["cue"]["revision"])
    response.headers["ETag"] = revision
    return HotCueMutationOut(**{**result, "revision": revision})


def _raise_store_error(exc: cue_store.CueStoreError) -> NoReturn:
    headers = (
        {"ETag": exc.current_revision} if exc.current_revision is not None else None
    )
    raise HTTPException(status_code=exc.status, detail=exc.detail(), headers=headers) from exc


def _state_db_path(request: Request) -> Path:
    path = getattr(request.app.state, "state_db_path", None)
    return Path(path) if path is not None else rb_config.STATE_DB


def _rekordbox_content(stable_id: str) -> rb_vendor.RbContent | None:
    """The track's rekordbox row, ``None`` when it has no mapping.

    Raises 404 ``TRACK_NOT_FOUND`` for an unknown or removed track, exactly as
    the rekordbox-only surface did.
    """
    try:
        return rb_vendor.resolve_content(stable_id)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        if detail.get("code") != "VENDOR_MAPPING_NOT_FOUND":
            raise
        return None


def _vendor_cues(content: rb_vendor.RbContent | None) -> cue_store.VendorCues:
    """The rekordbox cues a track with no own row falls back to (and seeds from)."""
    if content is None:
        return list
    vendor_id = content.vendor_id
    return lambda: rb_vendor.fetch_cues(vendor_id)


def _duration_ms(conn: sqlite3.Connection, stable_id: str, content: Any) -> int | None:
    row = conn.execute(
        "SELECT duration_ms FROM tracks WHERE stable_id = ? AND deleted_at IS NULL", (stable_id,)
    ).fetchone()
    if row is not None and row[0] is not None:
        return int(row[0])
    length_s = getattr(content, "length_s", None)
    return int(length_s) * 1000 if length_s is not None else None


def _write_op(
    request: Request,
    stable_id: str,
    op: Callable[[sqlite3.Connection, cue_store.StoredCues, Any, cue_store.WriteBlob], dict[str, Any]],
) -> dict[str, Any]:
    """Run one read-modify-write of the track's cue set under BEGIN IMMEDIATE."""
    content = _rekordbox_content(stable_id)
    conn = state_db.open_rw(_state_db_path(request))
    try:
        conn.execute("BEGIN IMMEDIATE")
        with StateWriter(conn, actor="webui") as writer:
            state = cue_store.load(conn, stable_id, _vendor_cues(content))

            def write(blob: dict[str, Any]) -> None:
                writer.set_field(
                    stable_id,
                    cue_store.CUE_FIELD,
                    blob,
                    source=cue_store.OWN_SOURCE,
                    modified_at=canonical_now(),
                )

            result = op(conn, state, content, write)
        conn.commit()
    except cue_store.CueStoreError as exc:
        conn.rollback()
        _raise_store_error(exc)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    publish("library.changed", {"kind": "hot_cues", "ids": [stable_id]})
    return result


@router.get("/{stable_id}/hot-cues", response_model=list[HotCueSlotOut])
def list_hot_cue_slots(
    stable_id: str,
    request: Request,
    _backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> list[HotCueSlotOut]:
    content = _rekordbox_content(stable_id)
    conn = state_db.open_ro(_state_db_path(request))
    try:
        state = cue_store.load(conn, stable_id, _vendor_cues(content))
    finally:
        conn.close()
    try:
        slots = cue_store.hot_cue_slots(state)
    except cue_store.CueStoreError as exc:
        _raise_store_error(exc)
    return [HotCueSlotOut(**slot) for slot in slots]


@router.put(
    "/{stable_id}/hot-cues/{slot}",
    response_model=HotCueMutationOut,
    responses=_MUTATION_RESPONSES,
    openapi_extra={"parameters": [_IF_MATCH_OPENAPI_PARAMETER]},
)
def save_hot_cue(
    stable_id: str,
    slot: HotCueSlot,
    body: HotCueSaveIn,
    request: Request,
    response: Response,
    if_match: str = Depends(_require_if_match),
    _backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> HotCueMutationOut:
    result = _write_op(
        request,
        stable_id,
        lambda conn, state, content, write: cue_store.save_hot_cue(
            state,
            slot,
            body.in_ms,
            expected_revision=if_match,
            comment=body.comment,
            color_table_index=body.color_table_index,
            duration_ms=_duration_ms(conn, stable_id, content),
            write=write,
        ),
    )
    return _mutation_response(result, response)


@router.delete(
    "/{stable_id}/hot-cues/{slot}",
    response_model=HotCueMutationOut,
    responses=_MUTATION_RESPONSES,
    openapi_extra={"parameters": [_IF_MATCH_OPENAPI_PARAMETER]},
)
def clear_hot_cue(
    stable_id: str,
    slot: HotCueSlot,
    request: Request,
    response: Response,
    if_match: str = Depends(_require_if_match),
    _backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> HotCueMutationOut:
    result = _write_op(
        request,
        stable_id,
        lambda _conn, state, _content, write: cue_store.clear_hot_cue(
            state, slot, expected_revision=if_match, write=write
        ),
    )
    return _mutation_response(result, response)


@router.put(
    "/{stable_id}/hot-cues/{slot}/restore",
    response_model=HotCueMutationOut,
    responses=_MUTATION_RESPONSES,
    openapi_extra={"parameters": [_IF_MATCH_OPENAPI_PARAMETER]},
)
def restore_hot_cue(
    stable_id: str,
    slot: HotCueSlot,
    body: HotCueRestoreIn,
    request: Request,
    response: Response,
    if_match: str = Depends(_require_if_match),
    _backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> HotCueMutationOut:
    result = _write_op(
        request,
        stable_id,
        lambda _conn, state, _content, write: cue_store.restore_hot_cue(
            state,
            slot,
            expected_revision=if_match,
            reversal_id=body.reversal_id,
            write=write,
        ),
    )
    return _mutation_response(result, response)


__all__ = ["AnlzCueOut", "HotCueMutationOut", "HotCueSaveIn", "HotCueSlotOut", "router"]
