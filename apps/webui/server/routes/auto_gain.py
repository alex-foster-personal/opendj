"""Auto gain per track: what the deck applies at load, and the user's own value.

  GET /api/v1/auto-gain/tracks/{id}              resolved offset + why (AUTOGAIN-01..05)
  PUT /api/v1/auto-gain/tracks/{id}/user-offset  set or clear the user's dB (AUTOGAIN-04)

The deck reads the GET at every load and applies ``effective_offset_db`` at
the trim stage, under the TRIM knob. The analysed value comes from the
``loudness`` lane (integrated LUFS and true peak, BS.1770), the target and
on/off from the ``auto_gain`` UI pref, and the user value from the track field
``auto_gain_user_db``, which no analysis writes.

CLI twin: ``opendj track auto-gain <id> [set <dB>|clear]``; the pref is
``opendj api PUT /api/v1/ui-prefs`` with ``{"auto_gain": {...}}``.

-Claude
"""

from __future__ import annotations

import math
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from apps.analysis_loudness.http import auto_gain
from apps.shared.events import publish

from ..backend import StateBackend, Track
from ..deps import get_read_state, get_write_state
from ..etag import compute_etag
from . import ui_prefs

router = APIRouter(prefix="/auto-gain", tags=["auto-gain"])

USER_FIELD = "auto_gain_user_db"


class AutoGainOut(BaseModel):
    stable_id: str
    status: Literal["analysed", "user", "not_analysed", "off"]
    enabled: bool
    target_lufs: float
    integrated_lufs: float | None
    true_peak_dbtp: float | None
    analysed_offset_db: float | None
    user_offset_db: float | None
    effective_offset_db: float
    limited_by: Literal["range", "true_peak"] | None
    #: Why the loudness is not usable, when it is not (lane status/reason).
    loudness_reason: str | None
    offset_min_db: float = auto_gain.OFFSET_MIN_DB
    offset_max_db: float = auto_gain.OFFSET_MAX_DB
    true_peak_ceiling_dbtp: float = auto_gain.TRUE_PEAK_CEILING_DBTP


class UserOffsetIn(BaseModel):
    #: dB, or null to drop the user's value and use the analysed one again.
    offset_db: float | None


def _lane_number(track: Track, field: str) -> tuple[float | None, str | None]:
    """An ``ok`` numeric lane value, or None with the reason it is not usable."""
    view = track.provenance.get(field)
    if view is None:
        return None, f"{field} not analysed"
    if view.status != "ok":
        return None, view.reason or f"{field} {view.status}"
    value: Any = view.value
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None, f"{field} is not a finite number"
    return float(value), None


def resolve_for_track(track: Track, prefs: dict[str, Any]) -> AutoGainOut:
    lufs, reason = _lane_number(track, "loudness_lufs")
    peak, _ = _lane_number(track, "loudness_dbtp")
    resolved = auto_gain.compute_auto_gain(
        enabled=prefs["enabled"],
        target_lufs=prefs["target_lufs"],
        integrated_lufs=lufs,
        true_peak_dbtp=peak,
        user_offset_db=track.auto_gain_user_db,
    )
    return AutoGainOut(
        stable_id=track.stable_id,
        status=resolved.status,
        enabled=resolved.enabled,
        target_lufs=resolved.target_lufs,
        integrated_lufs=resolved.integrated_lufs,
        true_peak_dbtp=resolved.true_peak_dbtp,
        analysed_offset_db=resolved.analysed_offset_db,
        user_offset_db=resolved.user_offset_db,
        effective_offset_db=resolved.effective_offset_db,
        limited_by=resolved.limited_by,
        loudness_reason=reason,
    )


def _prefs(request: Request) -> dict[str, Any]:
    return ui_prefs.get_ui_prefs(request).auto_gain.model_dump()


@router.get("/tracks/{stable_id}", response_model=AutoGainOut)
def get_track_auto_gain(
    stable_id: str,
    request: Request,
    backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> AutoGainOut:
    """The offset a deck applies when it loads this track, and why."""
    track = backend.get_track(stable_id)  # NotFoundError -> 404 (app handler)
    return resolve_for_track(track, _prefs(request))


@router.put("/tracks/{stable_id}/user-offset", response_model=AutoGainOut)
def put_track_auto_gain_user_offset(
    stable_id: str,
    body: UserOffsetIn,
    request: Request,
    backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> AutoGainOut:
    """Set (or clear with null) the user's own offset. It wins over the analysed
    one and is never replaced by re-analysis."""
    if body.offset_db is not None:
        try:
            auto_gain.validate_user_offset_db(body.offset_db)
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail={"code": "AUTO_GAIN_INVALID", "message": str(exc)}
            ) from exc
    current = backend.get_track(stable_id)  # NotFoundError -> 404 (app handler)
    updated = backend.update_track(
        stable_id,
        {USER_FIELD: body.offset_db},
        expected_etag=compute_etag(current.stable_id, current.updated_at, current.selection_tag),
        source="webui",
    )
    publish("library.changed", {"kind": "tracks", "ids": [stable_id]})
    return resolve_for_track(updated, _prefs(request))
