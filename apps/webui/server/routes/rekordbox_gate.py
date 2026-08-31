"""Read-only status of the one-way import gate (agent-native parity).

``GET /api/v1/rekordbox/writeback-gate`` is the ONE thing the UI and any agent
driving this daemon need in order to know, before firing a request, whether a
write toward real rekordbox data can run at all.  It exists so a disabled
control and the server refusing it can never disagree about the reason.

The gate itself, the write-surface map, and the refusal semantics all live in
:mod:`apps.shared.rekordbox_writeback`.  This route reports; it never decides.

Requirements (mini-PRD):
  * ✔︎ 🎯 reports ``enabled`` from the process env at request time, never a
    boot-time snapshot, so flipping the flag does not need a restart to be
    observable.
    [if] the endpoint answers enabled=true while the env says off [then ⛔️]
  * ✔︎ 🎯 always returns the refusal code, message, and UI tooltip so a client
    never hardcodes its own wording.
    [if] the payload's ui_title drifts from UI_REFUSAL_TITLE [then ⛔️]
  * ✔︎ 🎯 lists every mapped write surface, so an agent can enumerate what is
    switched off without reading the source.
    [if] a surface is added to the map and this route stops listing it [then ⛔️]
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from apps.shared.rekordbox_writeback import (
    REKORDBOX_WRITEBACK_ENABLED_ENV,
    UI_REFUSAL_TITLE,
    WRITE_SURFACES,
    WRITEBACK_DISABLED_CODE,
    WRITEBACK_DISABLED_MESSAGE,
    writeback_enabled,
)

router = APIRouter(prefix="/rekordbox", tags=["rekordbox-gate"])


class WriteSurfaceOut(BaseModel):
    surface_id: str
    kind: str
    entrypoint: str
    target: str


class WritebackGateOut(BaseModel):
    enabled: bool
    code: str
    message: str
    ui_title: str
    env_var: str
    surfaces: list[WriteSurfaceOut]


@router.get("/writeback-gate", response_model=WritebackGateOut)
def read_writeback_gate() -> WritebackGateOut:
    return WritebackGateOut(
        enabled=writeback_enabled(),
        code=WRITEBACK_DISABLED_CODE,
        message=WRITEBACK_DISABLED_MESSAGE,
        ui_title=UI_REFUSAL_TITLE,
        env_var=REKORDBOX_WRITEBACK_ENABLED_ENV,
        surfaces=[
            WriteSurfaceOut(
                surface_id=surface.surface_id,
                kind=surface.kind,
                entrypoint=surface.entrypoint,
                target=surface.target,
            )
            for surface in WRITE_SURFACES
        ],
    )


__all__ = ["WriteSurfaceOut", "WritebackGateOut", "router"]
