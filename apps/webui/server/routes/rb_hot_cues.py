"""Hot-cue SAVE/CLEAR write endpoints -- the edit-write-path lane's first
consumer of the rb_vendor.py write surface (djmdCue Kind 1-8 only; see
:func:`apps.webui.server.rb_vendor.save_hot_cue` for the Kind 9-11
rationale -- this router never exposes slots beyond H).

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

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from apps.shared.events import publish

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


@router.get("/{stable_id}/hot-cues", response_model=list[HotCueSlotOut])
def list_hot_cue_slots(
    stable_id: str,
    _backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> list[HotCueSlotOut]:
    try:
        content = rb_vendor.resolve_content(stable_id)
        slots = rb_vendor.fetch_hot_cue_slots(content.vendor_id)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        if detail.get("code") != "VENDOR_MAPPING_NOT_FOUND":
            raise
        # Locally imported track (no rekordbox mapping): eight empty slots so
        # the deck-load path gets a non-null list instead of a 404.
        slots = rb_vendor.empty_hot_cue_slots()
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
    response: Response,
    if_match: str = Depends(_require_if_match),
    _backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> HotCueMutationOut:
    content = rb_vendor.resolve_content(stable_id)
    row = rb_vendor.save_hot_cue(
        content.vendor_id,
        slot,
        body.in_ms,
        expected_revision=if_match,
        comment=body.comment,
        color_table_index=body.color_table_index,
    )
    publish("library.changed", {"kind": "hot_cues", "ids": [stable_id]})
    return _mutation_response(row, response)


@router.delete(
    "/{stable_id}/hot-cues/{slot}",
    response_model=HotCueMutationOut,
    responses=_MUTATION_RESPONSES,
    openapi_extra={"parameters": [_IF_MATCH_OPENAPI_PARAMETER]},
)
def clear_hot_cue(
    stable_id: str,
    slot: HotCueSlot,
    response: Response,
    if_match: str = Depends(_require_if_match),
    _backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> HotCueMutationOut:
    content = rb_vendor.resolve_content(stable_id)
    result = rb_vendor.clear_hot_cue(
        content.vendor_id, slot, expected_revision=if_match,
    )
    publish("library.changed", {"kind": "hot_cues", "ids": [stable_id]})
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
    response: Response,
    if_match: str = Depends(_require_if_match),
    _backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> HotCueMutationOut:
    content = rb_vendor.resolve_content(stable_id)
    result = rb_vendor.restore_hot_cue(
        content.vendor_id,
        slot,
        expected_revision=if_match,
        reversal_id=body.reversal_id,
    )
    publish("library.changed", {"kind": "hot_cues", "ids": [stable_id]})
    return _mutation_response(result, response)


__all__ = ["router", "HotCueSaveIn", "AnlzCueOut", "HotCueMutationOut", "HotCueSlotOut"]
