"""Hot-cue SAVE/CLEAR/RESTORE endpoints over Open DJ's own cue store (CUES-01).

Cues live in ``state.db`` (``apps/shared/state/cue_store.py``), not in
rekordbox's ``djmdCue``, so every track can keep cues, rekordbox-mapped or
not. A track whose cues were never copied in falls back to its rekordbox cues
for reads, and its first edit seeds the own set from them. Nothing here writes
to rekordbox; write-back is the separate write-back-rekordbox-djay node.

The letter API stays A-H; ``djmdCue.Kind`` integers for slots D-H are unproven
until issue #4069's rekordbox-UI experiment (see ``cue_kind.KIND_BY_SLOT``).

  * ``GET    /api/v1/tracks/{stable_id}/hot-cues`` -> eight slot revisions
  * ``PUT    /api/v1/tracks/{stable_id}/hot-cues/{slot}`` -> mutation + reversal token
    (body ``out_ms`` saves a loop hot cue, HOTCUE-LOOP-01)
  * ``DELETE /api/v1/tracks/{stable_id}/hot-cues/{slot}`` -> mutation + reversal token
  * ``PUT    /api/v1/tracks/{stable_id}/hot-cues/{slot}/restore`` -> CAS restore
  * ``GET    /api/v1/tracks/{stable_id}/memory-cues`` -> memory cues/loops + loop revision
  * ``PUT    /api/v1/tracks/{stable_id}/memory-cues/{in_ms}/active`` -> flag the one
    active memory loop (body ``{active: bool}``, same If-Match rule)

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
from pathlib import Path as FsPath
from typing import Annotated, Any, Literal, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from apps.adapters.rekordbox import config as rb_config
from apps.shared.events import publish
from apps.shared.state import cue_store
from apps.shared.state import db as state_db
from apps.shared.state.sync_stamp import canonical_now
from apps.shared.state.writer import StateWriter

from .. import rb_vendor
from ..backend import StateBackend
from ..deps import get_read_state, get_write_state

router = APIRouter(prefix="/tracks", tags=["rb-hot-cues"])
MOUNT_UNDER_API_PREFIX = True

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
    out_ms: StrictInt | None = Field(
        None,
        description="Loop end in ms (HOTCUE-LOOP-01). Set it to save a loop "
                    "hot cue, the loop that was active when the cue was set; "
                    "must be after in_ms and inside the track. Omit or null "
                    "for a point cue.",
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


def _state_db_path(request: Request) -> FsPath:
    path = getattr(request.app.state, "state_db_path", None)
    return FsPath(path) if path is not None else rb_config.STATE_DB


def _rekordbox_content(stable_id: str) -> rb_vendor.RbContent | None:
    """The track's rekordbox row, ``None`` when it has no mapping.

    Raises 404 ``TRACK_NOT_FOUND`` for an unknown or removed track, exactly as
    the rekordbox-only surface did. ``MASTER_DB_UNAVAILABLE`` (no rekordbox
    database on this machine, e.g. a CloudSync-delivered mapping) means no
    vendor cues, not an error: own cues never need rekordbox. The track check
    runs before the master DB is opened, so an unknown track is still a 404.
    """
    try:
        return rb_vendor.resolve_content(stable_id)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        if detail.get("code") not in ("VENDOR_MAPPING_NOT_FOUND", "MASTER_DB_UNAVAILABLE"):
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
    # 0 is how an unanalyzed import records "length unknown", not a real
    # length: bounding by it would refuse every cue on the track.
    if row is not None and row[0]:
        return int(row[0])
    length_s = getattr(content, "length_s", None)
    return int(length_s) * 1000 if length_s is not None else None


def _write_op(
    request: Request,
    stable_id: str,
    op: Callable[[sqlite3.Connection, cue_store.StoredCues, Any, cue_store.WriteBlob], dict[str, Any]],
) -> dict[str, Any]:
    """Run one read-modify-write of the track's cue set under a writer unit."""
    content = _rekordbox_content(stable_id)
    conn = state_db.open_rw(_state_db_path(request))
    try:
        with StateWriter(conn, actor="webui") as writer, writer.transaction():
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
    except cue_store.CueStoreError as exc:
        if conn.in_transaction:
            conn.rollback()
        _raise_store_error(exc)
    except Exception:
        if conn.in_transaction:
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
    _backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
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
            out_ms=body.out_ms,
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


# ---------- active memory loop (CUES-9967) ----------------------------------


class MemoryCueOut(BaseModel):
    """One memory cue or memory loop in the COMPONENT-MAP 2.3 cue shape."""

    kind: Literal["memory", "loop"]
    slot: Literal[None]
    in_ms: int
    out_ms: int | None
    is_loop: bool
    active_loop: bool
    beat_loop_size: int | None
    color_table_index: int | None
    comment: str | None


class MemoryCuesOut(BaseModel):
    cues: list[MemoryCueOut]
    revision: str
    active: MemoryCueOut | None = None


class ActiveLoopIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active: StrictBool


@router.get("/{stable_id}/memory-cues", response_model=MemoryCuesOut)
def list_memory_cues(
    stable_id: str,
    request: Request,
    response: Response,
    _backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> MemoryCuesOut:
    content = _rekordbox_content(stable_id)
    conn = state_db.open_ro(_state_db_path(request))
    try:
        state = cue_store.load(conn, stable_id, _vendor_cues(content))
    finally:
        conn.close()
    view = cue_store.memory_cues_view(state)
    response.headers["ETag"] = view["revision"]
    return MemoryCuesOut(**view, active=cue_store.active_memory_loop(state))


@router.put(
    "/{stable_id}/memory-cues/{in_ms}/active",
    response_model=MemoryCuesOut,
    responses={**_MUTATION_RESPONSES, 404: {"description": "No cue starts at in_ms"}},
    openapi_extra={"parameters": [_IF_MATCH_OPENAPI_PARAMETER]},
)
def set_active_memory_loop(
    stable_id: str,
    in_ms: Annotated[int, Path(ge=0)],
    body: ActiveLoopIn,
    request: Request,
    response: Response,
    if_match: str = Depends(_require_if_match),
    _backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> MemoryCuesOut:
    result = _write_op(
        request,
        stable_id,
        lambda _conn, state, _content, write: cue_store.set_active_loop(
            state, in_ms, body.active, expected_revision=if_match, write=write
        ),
    )
    response.headers["ETag"] = result["revision"]
    return MemoryCuesOut(**result)


__all__ = [
    "ActiveLoopIn",
    "AnlzCueOut",
    "HotCueMutationOut",
    "HotCueSaveIn",
    "HotCueSlotOut",
    "MemoryCueOut",
    "MemoryCuesOut",
    "router",
]
