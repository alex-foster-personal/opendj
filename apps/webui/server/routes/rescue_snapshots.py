"""HTTP parity for the Gig rescue snapshot ring (RESCUE-01)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel

from apps.rescue.ring import (
    RescueSnapshotInvalidError,
    RescueSnapshotTooLargeError,
    append_snapshot,
    read_index,
    read_latest,
)
from apps.shared.paths import DATA_DIR

router = APIRouter(prefix="/performance/rescue-snapshots", tags=["performance-rescue"])


class RescueAppendOut(BaseModel):
    slot: int
    captured_at_ms: int
    bytes: int


def _data_dir(request: Request) -> Path:
    configured = getattr(request.app.state, "data_dir", None)
    return Path(configured) if configured is not None else DATA_DIR


@router.post("", status_code=202, response_model=RescueAppendOut)
async def post_rescue_snapshot(
    request: Request,
    payload: dict[str, Any] = Body(...),  # noqa: B008  # FastAPI DI
) -> RescueAppendOut:
    data_dir = _data_dir(request)
    try:
        result = await asyncio.to_thread(append_snapshot, data_dir, payload)
    except RescueSnapshotTooLargeError as error:
        raise HTTPException(status_code=413, detail=str(error)) from error
    except RescueSnapshotInvalidError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return RescueAppendOut.model_validate(result)


@router.get("/latest")
async def get_latest_rescue_snapshot(request: Request) -> dict[str, Any]:
    snapshot = await asyncio.to_thread(read_latest, _data_dir(request))
    if snapshot is None:
        raise HTTPException(status_code=404, detail="no rescue snapshot available")
    return snapshot


@router.get("/index")
async def get_rescue_snapshot_index(request: Request) -> dict[str, Any]:
    index = await asyncio.to_thread(read_index, _data_dir(request))
    return {
        "schema": index.schema,
        "newest_slot": index.newest_slot,
        "slots": [
            {"slot": slot.slot, "captured_at_ms": slot.captured_at_ms, "bytes": slot.bytes}
            for slot in index.slots
        ],
    }
