"""Where a track's tempo moves: `GET /api/v1/tracks/{stable_id}/tempo-stability`.

One contract for the deck strip's tempo lane, the big waveform's tempo-change
marks (issue #8339) and the library's BPM Stability column (issue #7879). The
verdict and every segment are decided here, from the grid `/anlz` serves this
track now (`served_anlz_payload`), so no reader computes one of its own.
CLI twin: `opendj track tempo <stable_id>`.

Requirements (mini-PRD):
  - Reads the SERVED grid: same source selection, overlays and grid edit as
    `/anlz`, by calling the function `/anlz` calls.
    [if] this route judges a different grid than the deck draws [then] broken
  - No analysis is `not_analyzed` with an empty lane, never a guess.
    [if] a track without beats returns a verdict or a segment [then] broken
    [if] a track with no ANLZ files 404s instead of `not_analyzed` [then] broken
  - The wire mirrors `apps.analysis_beatgrid.tempo_stability` field for field.
    [if] a field is computed here rather than copied [then] broken
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from apps.analysis_beatgrid.grid_quality import GridClass
from apps.analysis_beatgrid.tempo_stability import Direction, Kind, Verdict, tempo_stability
from apps.shared import runtime_policy

from ..backend import StateBackend
from ..deps import get_read_state
from .rb_assets import served_anlz_payload

router = APIRouter(prefix="/tracks", tags=["rb-assets"])

#: The served waveform is not read here; ask for the smallest one `/anlz` builds.
_POINTS = runtime_policy.ANLZ_POINTS_MIN
_NO_BEATS_REASON = "the served beatgrid has no beats"


class TempoSegmentOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    start_s: float
    end_s: float
    #: Mean BPM over the stretch.
    bpm: float
    #: Smoothed BPM of its first and last bar (a ramp's two ends).
    bpm_start: float
    bpm_end: float
    #: `bpm` relative to `nominal_bpm`, percent, signed.
    delta_pct: float
    kind: Kind
    #: 0..1; 1 is 15 percent or more off nominal (#7879's deep-red floor).
    severity: float = Field(ge=0.0, le=1.0)
    direction: Direction


class TempoStabilityOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    stable_id: str
    status: Literal["ok", "not_analyzed"]
    #: Why `not_analyzed`; None when `ok`.
    reason: str | None
    #: Which grid was judged: the one the deck draws.
    beatgrid_source: str | None
    nominal_bpm: float | None
    verdict: Verdict | None
    #: Furthest any bar gets from nominal, percent, unsigned.
    largest_change_pct: float | None
    #: Changepoints the NATIVE-03 detector accepts on this grid.
    change_count: int | None
    grid_class: GridClass | None
    grid_quality_message: str | None
    #: Empty when steady or not analyzed; otherwise tiles the grid.
    segments: list[TempoSegmentOut]


def _not_analyzed(stable_id: str, reason: str, source: str | None) -> TempoStabilityOut:
    return TempoStabilityOut(
        stable_id=stable_id,
        status="not_analyzed",
        reason=reason,
        beatgrid_source=source,
        nominal_bpm=None,
        verdict=None,
        largest_change_pct=None,
        change_count=None,
        grid_class=None,
        grid_quality_message=None,
        segments=[],
    )


@router.get("/{stable_id}/tempo-stability", response_model=TempoStabilityOut)
def get_track_tempo_stability(
    request: Request,
    response: Response,
    stable_id: str,
    _backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> TempoStabilityOut:
    # Moves with the lane selection, a landed own record and every grid edit,
    # none of which a validator here could see (same reason as /grid-provenance).
    response.headers["Cache-Control"] = "no-store"
    try:
        payload = served_anlz_payload(request, stable_id, _POINTS)
    except HTTPException as exc:
        detail: dict[str, Any] = exc.detail if isinstance(exc.detail, dict) else {}
        if detail.get("code") != "ANALYSIS_NOT_FOUND":
            raise
        return _not_analyzed(stable_id, str(detail.get("message") or "no analysis"), None)
    beatgrid = payload["beatgrid"]
    beats = beatgrid["beats"]
    if len(beats) < 2:
        return _not_analyzed(stable_id, _NO_BEATS_REASON, beatgrid["source"])
    result = tempo_stability([beat["t"] for beat in beats], [beat["bpm"] for beat in beats])
    return TempoStabilityOut(
        stable_id=stable_id,
        status="ok",
        reason=None,
        beatgrid_source=beatgrid["source"],
        nominal_bpm=result.nominal_bpm,
        verdict=result.verdict,
        largest_change_pct=result.largest_change_pct,
        change_count=result.change_count,
        grid_class=result.grid_class,
        grid_quality_message=result.grid_quality_message,
        segments=[TempoSegmentOut(**asdict(segment)) for segment in result.segments],
    )
