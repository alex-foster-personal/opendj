"""Mixed In Key energy coverage and backfill over HTTP (MIKBF-04).

Every route has a CLI twin (``python -m apps.mik backfill <step>``) and an MCP
tool (``apps/opendj_cli/mcp_energy_tools.py``); all three return the document
:mod:`apps.mik.backfill` builds.

    GET  /api/v1/energy/status              coverage against the ``present`` denominator
    POST /api/v1/energy/backfill/plan       files that need a fresh analysis
    POST /api/v1/energy/backfill/stage      copy the next files into a batch folder
    POST /api/v1/energy/backfill/collect    load a batch from a COPY of the MIK store

``stage`` and ``collect`` are dry runs unless the body says ``live``. Neither
launches Mixed In Key. A batch folder named over HTTP must be one ``stage``
made: a direct child of the configured ingest root carrying the batch prefix.
The equivalence gate's override is deliberately NOT reachable from here; a
field the gate has not passed is reported under ``blocked_by_gate``.

-Claude
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from apps.mik import backfill, backfill_settings, backfill_share
from apps.mik.load_types import LoadError
from apps.mik.mikdb import MikReadError
from apps.shared.equivalence import EquivalenceGateError
from apps.shared.paths import STATE_DB

from ..backend import StateBackend
from ..deps import get_backend, get_library_data_dir, get_lock_status, get_write_state

router = APIRouter(prefix="/energy", tags=["energy"])
MOUNT_UNDER_API_PREFIX = True

REFUSED_CODE = "energy_backfill_refused"


def _library(request: Request) -> backfill.Library:
    path = Path(getattr(request.app.state, "state_db_path", STATE_DB))
    if not path.is_file():
        raise HTTPException(503, {"code": "no_library_db", "message": f"no library database at {path}"})
    return backfill.Library(state_db=path, data_dir=get_library_data_dir(request))


def _refused(exc: Exception) -> HTTPException:
    return HTTPException(409, {"code": REFUSED_CODE, "message": str(exc)})


_HANDLED = (LoadError, MikReadError, EquivalenceGateError, FileNotFoundError)


def _store(raw: str | None) -> Path | None:
    return Path(raw).expanduser() if raw else None


def _batch_dir(request: Request, raw: str) -> Path:
    """Only a folder ``stage`` could have made: ``<ingest root>/mik-batch-*``.

    Compared after resolving, and a symlinked batch folder is refused: a link
    under the ingest root to a folder elsewhere would let collect read, and
    ``delete_copies`` unlink, files outside it.
    """
    folder = Path(raw).expanduser()
    root = backfill_settings.load(get_library_data_dir(request)).ingest_root
    inside = folder.resolve().parent == root.resolve() and not folder.is_symlink()
    if not inside or folder.parent != root or not folder.name.startswith(backfill.CFG.batch_prefix):
        raise HTTPException(
            422,
            {
                "code": "not_a_batch_folder",
                "message": f"{folder} is not a {backfill.CFG.batch_prefix}* folder directly under {root}",
            },
        )
    return folder


# ------------------------------------------------------------------ models
class PlanIn(BaseModel):
    limit: int | None = Field(default=None, ge=1, le=backfill.CFG.max_limit)
    store: str | None = Field(
        default=None, description="A COPY of a MIK store not yet loaded; rows it would settle are left out."
    )
    share_across_duplicates: bool = Field(
        default=False,
        description="Also treat same artist + title with durations within 1 s as one recording.",
    )


class StageIn(PlanIn):
    live: bool = Field(default=False, description="Copy the files. Without it this is a dry run.")
    min_free_bytes: int | None = Field(
        default=None, ge=0, description="Free disk the copies must leave. Default: the library's stored setting."
    )


class ShareIn(BaseModel):
    live: bool = Field(default=False, description="Write to the library. Without it this is a dry run.")
    store: str | None = Field(
        default=None, description="A COPY of a MIK store not yet loaded; rows it would settle are held."
    )
    share_across_duplicates: bool = Field(
        default=False, description="Also treat same artist, title and duration (within 1 s) as one recording."
    )


class CollectIn(BaseModel):
    batch_dir: str = Field(description="The batch folder a live stage returned.")
    store: str = Field(description="A COPY of the MIK store, or MIK's own file with copy_store_first.")
    live: bool = Field(default=False, description="Write to the library. Without it this is a dry run.")
    delete_copies: bool = Field(default=False, description="With live: remove copies whose energy was loaded.")
    share_across_duplicates: bool = False
    copy_store_first: bool = Field(default=False, description="Copy the store and read only the copy.")


# ------------------------------------------------------------------ routes
@router.get("/status")
def energy_status(
    request: Request,
    share_across_duplicates: bool = Query(default=False),
) -> dict[str, Any]:
    try:
        return backfill.status(_library(request), share_across_duplicates=share_across_duplicates)
    except _HANDLED as exc:
        raise _refused(exc) from exc


@router.post("/backfill/plan")
def backfill_plan(request: Request, body: PlanIn) -> dict[str, Any]:
    try:
        return backfill.plan(
            _library(request),
            limit=body.limit,
            store=_store(body.store),
            share_across_duplicates=body.share_across_duplicates,
        )
    except _HANDLED as exc:
        raise _refused(exc) from exc


@router.post("/backfill/stage")
def backfill_stage(
    request: Request,
    body: StageIn,
    backend: StateBackend = Depends(get_backend),  # noqa: B008
    lock_status: dict[str, Any] | None = Depends(get_lock_status),  # noqa: B008
) -> dict[str, Any]:
    if body.live:
        # A live stage copies audio and claims files for a batch, so it waits
        # for the writer lock like collect does; a dry run only reads.
        get_write_state(request, backend, lock_status)
    try:
        return backfill.stage(
            _library(request),
            limit=body.limit,
            live=body.live,
            store=_store(body.store),
            share_across_duplicates=body.share_across_duplicates,
            min_free_bytes=body.min_free_bytes,
        )
    except _HANDLED as exc:
        raise _refused(exc) from exc


@router.post("/backfill/share")
def backfill_share_route(
    request: Request,
    body: ShareIn,
    backend: StateBackend = Depends(get_backend),  # noqa: B008
    lock_status: dict[str, Any] | None = Depends(get_lock_status),  # noqa: B008
) -> dict[str, Any]:
    """Give a complete donor's MIK energy and series to its empty present duplicates."""
    if body.live:
        # Only a live share writes the library; a dry run reads.
        get_write_state(request, backend, lock_status)
    try:
        return backfill_share.share(
            _library(request),
            live=body.live,
            share_across_duplicates=body.share_across_duplicates,
            store=_store(body.store),
        )
    except _HANDLED as exc:
        raise _refused(exc) from exc


@router.post("/backfill/collect")
def backfill_collect(
    request: Request,
    body: CollectIn,
    backend: StateBackend = Depends(get_backend),  # noqa: B008
    lock_status: dict[str, Any] | None = Depends(get_lock_status),  # noqa: B008
) -> dict[str, Any]:
    if body.live:
        # Only a live collect writes the library; a dry run reads, so it is
        # answered even while a peer holds the writer lock (as stage does).
        get_write_state(request, backend, lock_status)
    try:
        collect = backfill.CollectRequest(
            batch_dir=_batch_dir(request, body.batch_dir),
            store=Path(body.store).expanduser(),
            library_root=None,  # always the root stage recorded in the batch manifest
            live=body.live,
            delete_copies=body.delete_copies,
            share_across_duplicates=body.share_across_duplicates,
            copy_store_first=body.copy_store_first,
        )
        return backfill.collect(_library(request), collect)
    except _HANDLED as exc:
        raise _refused(exc) from exc
