"""One track's own BPM range for the beatgrid analysis over HTTP (ANALYSIS-11723).

  GET    /api/v1/tracks/{stable_id}/analysis-range   the range in force
  PUT    /api/v1/tracks/{stable_id}/analysis-range   {"range": "93-186"}: set it
  DELETE /api/v1/tracks/{stable_id}/analysis-range   back to the global 70-180 band

Setting the range re-analyzes nothing by itself; the re-analyze is the existing
order ``POST /api/v1/analysis-queue/orders/{stable_id}/beatgrid``, which the
analysis dots popover's "Re-analyze grid" item posts right after this write. The
CLI twin is ``opendj track analysis-range``.

-Claude
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from apps.shared.state import analysis_range
from apps.shared.state import db as state_db
from apps.shared.state.sync_stamp import canonical_now
from apps.shared.state.writer import StateWriter

from ..backend import StateBackend
from ..deps import get_read_state, get_write_state
from ..rb_vendor_pkg.own_lane_store import state_conn_ro
from .grid_edit import _require_track, state_db_path

router = APIRouter(prefix="/tracks", tags=["analysis-range"])
MOUNT_UNDER_API_PREFIX = True


class AnalysisRangeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: One of the presets; validated in the handler so a miss answers ANALYSIS_RANGE_INVALID.
    range: str


class AnalysisRangeOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    stable_id: str
    #: The track's own range; null when it uses the global band.
    range: str | None
    #: The range the beatgrid producer analyzes this track in.
    effective: str
    choices: list[str]


def _out(stable_id: str, stored: analysis_range.BpmRange | None) -> AnalysisRangeOut:
    return AnalysisRangeOut(
        stable_id=stable_id,
        range=stored,
        effective=stored or analysis_range.GLOBAL_RANGE,
        choices=list(analysis_range.BPM_RANGES),
    )


def _write(request: Request, stable_id: str, stored: analysis_range.BpmRange | None) -> AnalysisRangeOut:
    conn = state_db.open_rw(state_db_path(request))
    try:
        with StateWriter(conn, actor="webui") as writer, writer.transaction():
            _require_track(conn, stable_id)
            writer.set_field(
                stable_id,
                analysis_range.FIELD,
                analysis_range.blob(stored),
                source=analysis_range.OWN_SOURCE,
                modified_at=canonical_now(),
            )
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()
    return _out(stable_id, stored)


@router.get("/{stable_id}/analysis-range", response_model=AnalysisRangeOut)
def get_analysis_range(
    request: Request,
    stable_id: str,
    _backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> AnalysisRangeOut:
    conn = state_conn_ro(state_db_path(request))
    if conn is None:
        raise HTTPException(status_code=404, detail={"code": "TRACK_NOT_FOUND", "message": f"no track {stable_id}"})
    try:
        _require_track(conn, stable_id)
        stored = analysis_range.read(conn, stable_id)
    finally:
        conn.close()
    return _out(stable_id, stored)


@router.put("/{stable_id}/analysis-range", response_model=AnalysisRangeOut)
def set_analysis_range(
    request: Request,
    stable_id: str,
    body: AnalysisRangeIn,
    _backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> AnalysisRangeOut:
    """Analyze this track's beatgrid inside ``range`` from the next re-analyze on."""
    try:
        chosen = analysis_range.parse(body.range)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": analysis_range.INVALID_CODE,
                "message": str(exc),
                "allowed": list(analysis_range.BPM_RANGES),
            },
        ) from exc
    return _write(request, stable_id, chosen)


@router.delete("/{stable_id}/analysis-range", response_model=AnalysisRangeOut)
def clear_analysis_range(
    request: Request,
    stable_id: str,
    _backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> AnalysisRangeOut:
    return _write(request, stable_id, None)


__all__ = ["AnalysisRangeIn", "AnalysisRangeOut", "router"]
