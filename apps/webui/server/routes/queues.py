"""M3 triage queues endpoint (CAT-05)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..backend import StateBackend
from ..deps import get_read_state
from ..models import QueueItemOut, QueueOut

router = APIRouter(prefix="/queues", tags=["queues"])

_VALID_KINDS = {"dedup", "bad_beatgrid", "auto_cue"}


@router.get("/{kind}", response_model=QueueOut)
def get_queue(
    kind: str,
    backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> QueueOut:
    if kind not in _VALID_KINDS:
        raise HTTPException(
            status_code=404,
            detail=f"unknown queue kind: {kind}; expected one of {sorted(_VALID_KINDS)}",
        )
    items, note = backend.get_queue(kind)  # type: ignore[arg-type]
    return QueueOut(
        items=[QueueItemOut(stable_id=i.stable_id, kind=i.kind, payload=i.payload)
               for i in items],
        note=note,
    )
