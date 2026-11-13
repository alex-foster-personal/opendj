"""Out-of-process structure sidecar landings: tell loaded decks to refetch (issue #6496).

The All-In-One CLI (``python -m apps.analysis_structure run``) writes sidecars in
its own process, which has no handle on the engine's event hub. After it writes
them it POSTs the track ids here, and this route publishes the same
``library.changed`` kind ``sections`` event the in-process own-fast producer
emits (``apps.analysis_structure.fast_sidecar._announce``), so a deck holding one
of those tracks refetches ``/anlz`` once. Local-operator only: the CLI runs on
this machine, and nobody else should be able to make every deck refetch.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, StringConstraints

from apps.shared.events import publish
from apps.webui.server.local_operator import local_operator_refusal

router = APIRouter(prefix="/structure", tags=["structure"])
MOUNT_UNDER_API_PREFIX = True

#: Bound on one call's id list; the CLI sends a larger run in chunks below it.
MAX_NOTIFY_IDS = 1000

StableId = Annotated[str, StringConstraints(min_length=1, max_length=256)]


class SidecarsChangedIn(BaseModel):
    """Stable ids whose structure sidecar was just written out of process."""

    stable_ids: list[StableId] = Field(min_length=1, max_length=MAX_NOTIFY_IDS)


class SidecarsChangedOut(BaseModel):
    """What was announced: the event topic, its kind and the de-duplicated ids."""

    topic: str
    kind: str
    ids: list[str]


def require_local_operator(request: Request) -> None:
    """403 unless the caller is this machine's own operator (see ``local_operator``)."""
    reason = local_operator_refusal(request)
    if reason is not None:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "STRUCTURE_NOTIFY_LOCAL_ONLY",
                "message": f"structure sidecar notify is local-operator only: {reason}",
            },
        )


@router.post(
    "/sidecars-changed",
    response_model=SidecarsChangedOut,
    dependencies=[Depends(require_local_operator)],
)
def sidecars_changed(body: SidecarsChangedIn) -> SidecarsChangedOut:
    """Publish ``library.changed`` kind ``sections`` for sidecars another process wrote."""
    ids = list(dict.fromkeys(body.stable_ids))
    publish("library.changed", {"kind": "sections", "ids": ids})
    return SidecarsChangedOut(topic="library.changed", kind="sections", ids=ids)


__all__ = ["MOUNT_UNDER_API_PREFIX", "SidecarsChangedIn", "SidecarsChangedOut", "router"]
