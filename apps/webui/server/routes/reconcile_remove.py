"""Bulk remove of missing tracks from the library (LIBM-78b, issue #12316).

``POST /api/v1/reconcile/broken:remove`` takes the Missing Tracks rows the user
selected and soft-deletes them in ONE transaction, the way rekordbox's Missing
File Manager removes its selection. It is the write half of the read-only
``reconcile`` router, kept in its own module so that one stays read-only.

Guard: the broken scan runs again at apply time. Every selected row must still
be missing; a row that was relinked since the listing, or one that never had a
missing-file status, refuses the whole batch with 409 ``not_missing`` and the
offending ids, and nothing is written. The library rows are soft-deleted
through ``StateWriter.remove_from_library`` (memberships tombstoned, undo with
``POST /tracks/{stable_id}:undelete``); no file is touched on disk.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from apps.shared.events import publish
from apps.shared.state import db as state_db
from apps.shared.state.writer import StateWriter
from apps.shared.state.writer_tracks import TrackLifecycleResult

from ..backend import StateBackend
from ..deps import get_write_state
from .reconcile import _scan_broken_ids

router = APIRouter(prefix="/reconcile", tags=["reconcile"])
MOUNT_UNDER_API_PREFIX = True

#: One request removes at most this many rows; the Missing Tracks pane selects fewer.
MAX_BULK_REMOVE: int = 1000


class MissingRemoveIn(BaseModel):
    """The selected Missing Tracks rows, by stable id, in selection order."""

    stable_ids: list[str] = Field(min_length=1, max_length=MAX_BULK_REMOVE)


class MissingRemoveOut(BaseModel):
    """What the batch removed: the ids and every playlist that lost a membership."""

    removed: list[str]
    playlist_ids: list[str]


def _refuse(status: int, code: str, message: str, stable_ids: list[str] | None = None) -> HTTPException:
    detail: dict[str, object] = {"code": code, "message": message}
    if stable_ids is not None:
        detail["stable_ids"] = stable_ids
    return HTTPException(status_code=status, detail=detail)


def _remove_all(db_path: Path, stable_ids: list[str]) -> list[TrackLifecycleResult]:
    """Soft-delete every id in one transaction; any failure rolls the batch back."""
    conn = state_db.open_rw(db_path)
    try:
        with StateWriter(conn, actor="webui") as writer, writer.transaction():
            return [writer.remove_from_library(sid) for sid in stable_ids]
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


@router.post("/broken:remove", response_model=MissingRemoveOut)
def remove_missing_tracks(
    body: MissingRemoveIn,
    request: Request,
    backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> MissingRemoveOut:
    """Remove the selected still-missing tracks from the library; files on disk are never touched."""
    stable_ids = list(dict.fromkeys(body.stable_ids))
    _total, broken = _scan_broken_ids(backend)
    not_missing = [sid for sid in stable_ids if sid not in broken]
    if not_missing:
        raise _refuse(
            409,
            "not_missing",
            f"{len(not_missing)} selected track(s) are no longer missing (relinked, found again, or never "
            "missing); refresh Missing Tracks and select again. Nothing was removed.",
            not_missing,
        )
    db_path = Path(request.app.state.state_db_path)
    if not db_path.is_file():
        raise _refuse(503, "state_db_missing", f"state DB not found at {db_path}; removal needs a state.db")
    results = _remove_all(db_path, stable_ids)
    playlist_ids = sorted({m.playlist_id for r in results for m in r.memberships})
    publish("library.changed", {"kind": "tracks", "ids": stable_ids, "playlist_ids": playlist_ids})
    return MissingRemoveOut(removed=stable_ids, playlist_ids=playlist_ids)
