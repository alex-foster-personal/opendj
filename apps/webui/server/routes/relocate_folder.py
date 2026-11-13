"""Folder-level relocate with a preview before any write (LIBM-72, ``..relocate_folder_plan``).

  POST /relocate-folder/preview  {from_prefix, to_prefix}          -> the plan, writes nothing
  POST /relocate-folder/apply    {from_prefix, to_prefix, plan_id} -> rewrites the plan's rows
  GET  /relocate-folder/nearby/{stable_id}                         -> LIBM-73 nearby proposal

``nearby``: once one track is found again in another folder (its file exists at its recorded
path and this machine holds an earlier, now-dead local location for it), propose that folder
move for the missing tracks under its old folder. Writes nothing; the answer carries the
``from_prefix``/``to_prefix``/``plan_id`` that ``/relocate-folder/apply`` takes as is. A track
that was never relocated, is still missing, or was found in the same folder answers 409 with a
typed code.

Not under ``/relocate/``: ``routes/relocate.py`` mounts first and its ``POST
/relocate/{stable_id}/apply`` would answer ``/relocate/folder/apply`` with stable_id ``folder``.

Scope: state-layer ``file_path`` only. A track with a rekordbox vendor mapping is never planned
(its path lives in rekordbox's database; the per-track ``/relocate/{stable_id}/apply`` handles
that write with its own safety rails) and is counted as ``skipped_rekordbox``.

Apply re-plans and answers 409 ``RELOCATE_PLAN_STALE`` with nothing written when the plan no
longer matches its ``plan_id``; the rewrite itself is one atomic compare-and-swap batch, then
every row is read back. Rows with no file at the new path are skipped and reported.
CLI twins: ``opendj track relocate-folder OLD NEW [apply]``, ``opendj track relocate-nearby <stable_id>``.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from apps.shared.events import publish
from apps.shared.state import db as state_db
from apps.shared.state import locations as state_locations

from .. import rb_vendor
from ..backend import BatchConflictError, StateBackend, TrackUpdate
from ..deps import get_library_state_db, get_read_state, get_write_state
from ..etag import compute_etag
from ..relocate_folder_plan import (
    InvalidRelocatePrefix,
    LocatedTrack,
    NotAFolderMove,
    RelocatePlan,
    RelocatePlanRow,
    RelocateSourceRow,
    plan_folder_relocate,
    plan_nearby,
)

router = APIRouter(prefix="/relocate-folder", tags=["relocate"])
MOUNT_UNDER_API_PREFIX = True


class RelocateFolderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_prefix: str
    to_prefix: str


class RelocateFolderApplyIn(RelocateFolderIn):
    plan_id: str


class RelocateFolderRowOut(BaseModel):
    stable_id: str
    old_path: str
    new_path: str
    new_exists: bool


class RelocateFolderCounts(BaseModel):
    rows: int
    new_exists: int
    missing_target: int
    skipped_rekordbox: int


class RelocateFolderPreviewOut(BaseModel):
    plan_id: str
    from_prefix: str
    to_prefix: str
    rows: list[RelocateFolderRowOut]
    counts: RelocateFolderCounts


class RelocateFolderApplyOut(BaseModel):
    plan_id: str
    relocated: list[RelocateFolderRowOut]
    skipped: list[RelocateFolderRowOut]


class RelocateNearbyOut(BaseModel):
    stable_id: str
    previous_path: str
    current_path: str
    from_prefix: str
    to_prefix: str
    plan_id: str
    proposed: list[RelocateFolderRowOut]
    not_found: list[RelocateFolderRowOut]
    counts: RelocateFolderCounts


def _version(track_stable_id: str, updated_at: str, selection_tag: str) -> str:
    return compute_etag(track_stable_id, updated_at, selection_tag)


def _candidate_ids(state_db_path: Path) -> list[tuple[str, str]]:
    """(stable_id, file_path) for every live track that has a recorded path."""
    connection = state_db.open_ro(state_db_path)
    try:
        return [
            (row[0], row[1])
            for row in connection.execute(
                "SELECT stable_id, file_path FROM tracks "
                "WHERE deleted_at IS NULL AND file_path IS NOT NULL ORDER BY stable_id"
            )
        ]
    finally:
        connection.close()


def _source_rows(backend: StateBackend, state_db_path: Path) -> tuple[list[RelocateSourceRow], list[RelocateSourceRow]]:
    """(rows the planner may rewrite, rekordbox-mapped rows it only counts)."""
    candidates = _candidate_ids(state_db_path)
    ids = [stable_id for stable_id, _path in candidates]
    rekordbox_ids = set(rb_vendor.bulk_rb_meta(ids, state_db_path=state_db_path))
    tracks = backend.get_tracks_bulk([sid for sid in ids if sid not in rekordbox_ids], live_only=True)
    rows = [
        RelocateSourceRow(t.stable_id, t.file_path, _version(t.stable_id, t.updated_at, t.selection_tag))
        for t in tracks.values()
        if t.file_path
    ]
    return rows, [RelocateSourceRow(sid, path, "") for sid, path in candidates if sid in rekordbox_ids]


def _plan(backend: StateBackend, state_db_path: Path, body: RelocateFolderIn) -> tuple[RelocatePlan, int]:
    """The current plan for ``body`` and how many rekordbox-mapped tracks it left out."""
    rows, rekordbox_rows = _source_rows(backend, state_db_path)
    try:
        plan = plan_folder_relocate(
            rows,
            from_prefix=body.from_prefix,
            to_prefix=body.to_prefix,
            exists=os.path.isfile,
        )
        rekordbox_under_prefix = plan_folder_relocate(
            rekordbox_rows,
            from_prefix=body.from_prefix,
            to_prefix=body.to_prefix,
            exists=os.path.isfile,
        )
    except InvalidRelocatePrefix as exc:
        raise HTTPException(status_code=422, detail={"code": "RELOCATE_PREFIX_INVALID", "message": str(exc)}) from exc
    return plan, len(rekordbox_under_prefix.rows)


def _row_out(row: RelocatePlanRow) -> RelocateFolderRowOut:
    return RelocateFolderRowOut(
        stable_id=row.stable_id,
        old_path=row.old_path,
        new_path=row.new_path,
        new_exists=row.new_exists,
    )


@router.post("/preview", response_model=RelocateFolderPreviewOut, operation_id="preview_relocate_folder")
def preview_relocate_folder(
    body: RelocateFolderIn,
    backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
    state_db_path: Path = Depends(get_library_state_db),  # noqa: B008  # FastAPI DI
) -> RelocateFolderPreviewOut:
    """Every path a folder relocate would rewrite, and whether each new file exists. Writes nothing."""
    plan, skipped_rekordbox = _plan(backend, state_db_path, body)
    return RelocateFolderPreviewOut(
        plan_id=plan.plan_id,
        from_prefix=plan.from_prefix,
        to_prefix=plan.to_prefix,
        rows=[_row_out(row) for row in plan.rows],
        counts=RelocateFolderCounts(
            rows=len(plan.rows),
            new_exists=len(plan.writable),
            missing_target=len(plan.skipped),
            skipped_rekordbox=skipped_rekordbox,
        ),
    )


@router.post("/apply", response_model=RelocateFolderApplyOut, operation_id="apply_relocate_folder")
def apply_relocate_folder(
    body: RelocateFolderApplyIn,
    backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
    state_db_path: Path = Depends(get_library_state_db),  # noqa: B008  # FastAPI DI
) -> RelocateFolderApplyOut:
    """Rewrite the previewed rows whose new file exists, or 409 if the plan went stale."""
    plan, _skipped_rekordbox = _plan(backend, state_db_path, body)
    stale = HTTPException(
        status_code=409,
        detail={
            "code": "RELOCATE_PLAN_STALE",
            "message": "the library changed since the preview; preview again before applying",
        },
    )
    if plan.plan_id != body.plan_id:
        raise stale
    writable = plan.writable
    if writable:
        try:
            backend.update_tracks(
                [TrackUpdate(row.stable_id, {"file_path": row.new_path}, row.version) for row in writable],
                source="webui",
            )
        except BatchConflictError as exc:
            raise stale from exc
        reread = backend.get_tracks_bulk([row.stable_id for row in writable])
        unread = [row.stable_id for row in writable if reread[row.stable_id].file_path != row.new_path]
        if unread:
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "RELOCATE_STATE_READBACK_FAILED",
                    "message": f"{len(unread)} rewritten paths did not read back (first: {unread[:5]})",
                },
            )
        publish("library.changed", {"kind": "reconcile", "ids": [row.stable_id for row in writable]})
    return RelocateFolderApplyOut(
        plan_id=plan.plan_id,
        relocated=[_row_out(row) for row in writable],
        skipped=[_row_out(row) for row in plan.skipped],
    )


def _nearby_refusal(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": code, "message": message})


def _located_track(backend: StateBackend, state_db_path: Path, stable_id: str) -> LocatedTrack:
    """The track's current path and its latest earlier local location whose file is gone, or 409."""
    track = backend.get_track(stable_id)  # NotFoundError -> 404
    current = track.file_path
    if not current or not os.path.isfile(current):
        raise _nearby_refusal(
            "RELOCATE_NEARBY_TRACK_MISSING",
            f"{stable_id} has no file at its recorded path; locate it before asking for nearby files",
        )
    connection = state_db.open_ro(state_db_path)
    try:
        locations = state_locations.list_locations(connection, stable_id)
    finally:
        connection.close()
    previous = [
        loc.file_path
        for loc in locations
        if loc.kind == "local" and loc.file_path and loc.file_path != current and not os.path.isfile(loc.file_path)
    ]
    if not previous:
        raise _nearby_refusal(
            "RELOCATE_NEARBY_NOT_RELOCATED",
            f"{stable_id} was never relocated: no earlier location of it on this machine",
        )
    return LocatedTrack(stable_id=stable_id, previous_path=previous[-1], current_path=current)


@router.get("/nearby/{stable_id}", response_model=RelocateNearbyOut, operation_id="relocate_folder_nearby")
def relocate_folder_nearby(
    stable_id: str,
    backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
    state_db_path: Path = Depends(get_library_state_db),  # noqa: B008  # FastAPI DI
) -> RelocateNearbyOut:
    """Propose one located track's folder move for its missing siblings (LIBM-73). Writes nothing."""
    located = _located_track(backend, state_db_path, stable_id)
    rows, rekordbox_rows = _source_rows(backend, state_db_path)
    try:
        plan = plan_nearby(located, rows, exists=os.path.isfile)
        rekordbox_nearby = plan_nearby(located, rekordbox_rows, exists=os.path.isfile)
    except NotAFolderMove as exc:
        raise _nearby_refusal("RELOCATE_NEARBY_SAME_FOLDER", str(exc)) from exc
    except InvalidRelocatePrefix as exc:
        raise HTTPException(status_code=422, detail={"code": "RELOCATE_PREFIX_INVALID", "message": str(exc)}) from exc
    return RelocateNearbyOut(
        stable_id=stable_id,
        previous_path=located.previous_path,
        current_path=located.current_path,
        from_prefix=plan.from_prefix,
        to_prefix=plan.to_prefix,
        plan_id=plan.plan_id,
        proposed=[_row_out(row) for row in plan.writable],
        not_found=[_row_out(row) for row in plan.skipped],
        counts=RelocateFolderCounts(
            rows=len(plan.rows),
            new_exists=len(plan.writable),
            missing_target=len(plan.skipped),
            skipped_rekordbox=len(rekordbox_nearby.rows),
        ),
    )
