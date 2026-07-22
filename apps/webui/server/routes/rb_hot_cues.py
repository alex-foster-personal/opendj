"""Hot-cue SAVE/CLEAR write endpoints -- the edit-write-path lane's first
consumer of the rb_vendor.py write surface (djmdCue Kind 1-8 only; see
:func:`apps.webui.server.rb_vendor.save_hot_cue` for the Kind 9-11
rationale -- this router never exposes slots beyond H).

Contract:

  * ``PUT    /api/v1/tracks/{stable_id}/hot-cues/{slot}``
    ``{in_ms, comment?, color_table_index?}`` -> 200 ``AnlzCueOut``
  * ``DELETE /api/v1/tracks/{stable_id}/hot-cues/{slot}`` -> 204

``slot`` is a ``Literal['A'..'H']`` at the route level, so an unverified
Kind 9-11 slot is unreachable via the API rather than silently guessed at.
SAVE always overwrites the slot's existing row (last SAVE wins).

The integrator wires ``router`` into ``create_app()`` under ``/api/v1``,
alongside the existing rb_assets router (same ``/tracks`` prefix)::

    from .routes import rb_hot_cues as rb_hot_cues_routes
    app.include_router(rb_hot_cues_routes.router, prefix=api_prefix)
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from .. import rb_vendor
from ..backend import StateBackend
from ..deps import get_write_state

router = APIRouter(prefix="/tracks", tags=["rb-hot-cues"])

HotCueSlot = Literal["A", "B", "C", "D", "E", "F", "G", "H"]


class HotCueSaveIn(BaseModel):
    in_ms: int = Field(
        ge=0,
        description="Cue position in ms. Already quantized to the beatgrid "
                    "by the caller when quantize is enabled -- this "
                    "endpoint never guesses at beat alignment.",
    )
    comment: Optional[str] = None
    color_table_index: Optional[int] = Field(None, ge=0)


class AnlzCueOut(BaseModel):
    """Same shape as one COMPONENT-MAP 2.3 cue row (rb_vendor.fetch_cues)."""

    kind: Literal["hot_cue"]
    slot: HotCueSlot
    in_ms: int
    out_ms: Optional[int]
    is_loop: bool
    active_loop: bool
    beat_loop_size: Optional[int]
    color_table_index: Optional[int]
    comment: Optional[str]


@router.put("/{stable_id}/hot-cues/{slot}", response_model=AnlzCueOut)
def save_hot_cue(
    stable_id: str,
    slot: HotCueSlot,
    body: HotCueSaveIn,
    _backend: StateBackend = Depends(get_write_state),
) -> AnlzCueOut:
    content = rb_vendor.resolve_content(stable_id)
    row = rb_vendor.save_hot_cue(
        content.vendor_id,
        slot,
        body.in_ms,
        comment=body.comment,
        color_table_index=body.color_table_index,
    )
    return AnlzCueOut(**row)


@router.delete("/{stable_id}/hot-cues/{slot}",
                status_code=status.HTTP_204_NO_CONTENT)
def clear_hot_cue(
    stable_id: str,
    slot: HotCueSlot,
    _backend: StateBackend = Depends(get_write_state),
) -> None:
    content = rb_vendor.resolve_content(stable_id)
    rb_vendor.clear_hot_cue(content.vendor_id, slot)


__all__ = ["router", "HotCueSaveIn", "AnlzCueOut"]
