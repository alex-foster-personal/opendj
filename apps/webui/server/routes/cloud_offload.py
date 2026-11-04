"""Cloud offload HTTP surface (OFFLOAD, spec ``(private path)``).

    POST /api/v1/offload/tracks                    {stable_ids}          -> 202 operation
    POST /api/v1/offload/playlists/{id}                                  -> 202 operation
    POST /api/v1/offload/restore                   {stable_ids, mode}    -> 202 operation
    POST /api/v1/offload/playlists/{id}/restore    {mode}                -> 202 operation
    GET  /api/v1/offload/operations[/{op_id}]                            -> progress
    POST /api/v1/offload/operations/{op_id}/cancel                       -> operation
    GET  /api/v1/offload/tracks?stable_ids=a,b                           -> per-track state
    GET  /api/v1/offload/summary                                         -> counts, bytes freed
    POST /api/v1/offload/sweep                     {dry_run}             -> 202 operation

The context-menu entries, the CLI (``opendj offload ...``) and the MCP tools
(``offload_*``) all call these, so there is one implementation to drift.
``mode`` is ``temporary`` ("Download for 24h") or ``permanent`` ("Re-download
permanently"). Missing R2 credentials answer 503 naming the variables.

-Claude
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from apps.cloud import offload, offload_ledger
from apps.cloud.config import MissingEnvError

from ..backend import NotFoundError, StateBackend
from ..cloud_offload_service import Operation, service_for
from ..deps import get_read_state

router = APIRouter(prefix="/offload", tags=["cloud-offload"])

MAX_IDS = 5000


class TracksIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stable_ids: list[str] = Field(min_length=1, max_length=MAX_IDS)


class RestoreIn(TracksIn):
    mode: offload_ledger.RestoreMode


class PlaylistRestoreIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: offload_ledger.RestoreMode


class SweepIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dry_run: bool = False


def _submit(request: Request, kind: str, stable_ids: list[str], **kwargs: Any) -> dict[str, Any]:
    try:
        op = service_for(request.app).submit(kind, stable_ids, **kwargs)  # type: ignore[arg-type]
    except MissingEnvError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "OFFLOAD_CREDENTIALS_MISSING", "message": str(exc)},
        ) from exc
    return op.wire()


def _playlist_ids(backend: StateBackend, playlist_id: str) -> list[str]:
    try:
        items = list(backend.get_playlist(playlist_id).items)
    except NotFoundError as exc:
        raise HTTPException(
            status_code=404, detail={"code": "PLAYLIST_NOT_FOUND", "message": f"no playlist {playlist_id}"}
        ) from exc
    if not items:
        raise HTTPException(
            status_code=409, detail={"code": "PLAYLIST_EMPTY", "message": f"playlist {playlist_id} has no tracks"}
        )
    return items


def _operation(request: Request, op_id: str) -> Operation:
    op = service_for(request.app).get(op_id)
    if op is None:
        raise HTTPException(status_code=404, detail={"code": "OPERATION_NOT_FOUND", "message": op_id})
    return op


@router.post("/tracks", status_code=202, operation_id="offload_tracks")
def offload_tracks(body: TracksIn, request: Request) -> dict[str, Any]:
    """Upload, verify, then delete the local files of each track."""
    return _submit(request, "offload", body.stable_ids)


@router.post("/playlists/{playlist_id}", status_code=202, operation_id="offload_playlist")
def offload_playlist(
    playlist_id: str,
    request: Request,
    backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> dict[str, Any]:
    return _submit(request, "offload", _playlist_ids(backend, playlist_id), playlist_id=playlist_id)


@router.post("/restore", status_code=202, operation_id="offload_restore")
def restore_tracks(body: RestoreIn, request: Request) -> dict[str, Any]:
    return _submit(request, "restore", body.stable_ids, mode=body.mode)


@router.post("/playlists/{playlist_id}/restore", status_code=202, operation_id="offload_restore_playlist")
def restore_playlist(
    playlist_id: str,
    body: PlaylistRestoreIn,
    request: Request,
    backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> dict[str, Any]:
    ids = _playlist_ids(backend, playlist_id)
    return _submit(request, "restore", ids, mode=body.mode, playlist_id=playlist_id)


@router.post("/sweep", status_code=202, operation_id="offload_sweep")
def run_sweep(body: SweepIn, request: Request) -> dict[str, Any]:
    return _submit(request, "sweep", [], dry_run=body.dry_run)


@router.get("/operations", operation_id="offload_operations")
def list_operations(request: Request) -> list[dict[str, Any]]:
    return [op.wire() for op in service_for(request.app).operations()]


@router.get("/operations/{op_id}", operation_id="offload_operation")
def get_operation(op_id: str, request: Request) -> dict[str, Any]:
    return _operation(request, op_id).wire()


@router.post("/operations/{op_id}/cancel", operation_id="offload_cancel")
def cancel_operation(op_id: str, request: Request) -> dict[str, Any]:
    _operation(request, op_id)
    op = service_for(request.app).cancel(op_id)
    assert op is not None
    return op.wire()


@router.get("/tracks", operation_id="offload_status")
def track_states(
    request: Request,
    stable_ids: str = Query(..., description="comma-separated stable_ids"),  # FastAPI DI
) -> dict[str, Any]:
    ids = [sid for sid in stable_ids.split(",") if sid]
    if not ids or len(ids) > MAX_IDS:
        raise HTTPException(status_code=422, detail={"code": "BAD_STABLE_IDS", "message": "1..5000 ids"})
    states = offload.offload_states(service_for(request.app).data_dir, ids)
    return {sid: (states[sid].wire() if sid in states else None) for sid in ids}


@router.get("/summary", operation_id="offload_summary")
def get_summary(request: Request) -> dict[str, int]:
    data_dir = service_for(request.app).data_dir
    if not offload_ledger.ledger_path(data_dir).is_file():
        return {**{state: 0 for state in offload_ledger.OFFLOAD_STATES}, "bytes_freed": 0}
    with offload_ledger.open_ledger(data_dir) as conn:
        return offload_ledger.summary(conn)
