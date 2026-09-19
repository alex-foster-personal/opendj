"""PLAY IT sort action (play-it-sort-action, LANE djcopilot-router pane).

CONTRACT this router owns:

  POST /api/v1/play-it/{playlist_id}/solve
    body:  PlayItGoalIn  {duration_min, peak_at_min?, floor_energy?,
                           ceiling_energy?}
    200:   PlayItSolveOut {playlist_id, etag, previous_order, proposed_order,
                            unchanged, steps: [PlayItStepOut],
                            constraints_unmet: [PlayItUnmetOut], solve_ms}
    404:   ErrorBody {error: "not_found", message}          unknown playlist
    422:   ErrorBody {error: "insufficient_data", message,
                       details: {missing: {bpm: [ids], key: [ids]}}}
           when more than 5% of the playlist's tracks are missing bpm or
           key (mirrors ``apps.dj_copilot.play_it._check_coverage``; energy
           is reported but not gating -- same known sqlite-backend gap
           documented in ``routes/copilot.py``).
    422:   ErrorBody {error: "invalid_goal", message}        bad SetGoal input

This endpoint is pure compute over the read backend: it does not write
anything (no state.db / play_orders dependency), so it behaves identically
against ``InMemoryBackend`` fixtures and a live daemon. ``etag`` in the
response is ``compute_etag(playlist_id, playlist.updated_at)`` -- the same
value the (separately landing) playlists-write router's
``PUT /playlists/{id}/tracks`` expects as ``If-Match`` -- so a client can
solve, preview, and apply the proposed order (or reapply ``previous_order``
to undo) without a round trip to fetch a fresh etag first. Applying/undoing
is a plain call to that existing membership-replace endpoint; this router
does not duplicate it.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from apps.dj_copilot.pinning import PinUnsatisfiableError
from apps.dj_copilot.set_goal import SetGoal
from apps.dj_copilot.solver import suggest_order
from apps.shared.harmonic import TrackFeature, key_to_camelot

from ..backend import StateBackend, Track
from ..deps import get_read_state
from ..errors import ErrorBody
from ..etag import compute_etag

router = APIRouter(prefix="/play-it", tags=["play-it"])

_MISSING_FIELD_LIMIT: float = 0.05


class PlayItGoalIn(BaseModel):
    duration_min: int = Field(60, ge=30, le=240)
    peak_at_min: int | None = Field(None, ge=0)
    floor_energy: int = Field(3, ge=1, le=10)
    ceiling_energy: int = Field(9, ge=1, le=10)
    peak_pins: list[str] = Field(default_factory=list)
    opener_pins: list[str] = Field(default_factory=list)
    closer_pin: str | None = None


class PlayItStepOut(BaseModel):
    position: int
    stable_id: str
    title: str | None
    artist: str | None
    bpm: float | None
    key_camelot: str | None
    energy: int | None
    transition_hint: str
    camelot_distance: int | None
    bpm_delta_pct: float | None
    target_energy: float
    actual_energy: float
    pin_role: Literal["opener", "peak", "closer"] | None = None


class PlayItUnmetOut(BaseModel):
    kind: str
    position: int
    detail: dict[str, float | str]


class PlayItSolveOut(BaseModel):
    playlist_id: str
    etag: str
    previous_order: list[str]
    proposed_order: list[str]
    unchanged: bool
    steps: list[PlayItStepOut]
    constraints_unmet: list[PlayItUnmetOut]
    solve_ms: float


def _camelot_or_none(key: str | None) -> str | None:
    if key is None or not key.strip():
        return None
    try:
        return str(key_to_camelot(key))
    except ValueError:
        return None


def _energy_of(track: Track) -> int | None:
    prov = (track.provenance or {}).get("energy")
    if prov is None or prov.value is None:
        return None
    return int(prov.value)


def _track_to_feature(track: Track) -> TrackFeature:
    return TrackFeature(
        stable_id=track.stable_id,
        artist=track.artist,
        bpm=track.bpm,
        key_camelot=_camelot_or_none(track.key),
        energy=_energy_of(track),
    )


def _missing_fields(features: list[TrackFeature]) -> dict[str, list[str]] | None:
    """bpm/key coverage gate; energy is reported elsewhere but not gating
    (SqliteBackend does not project it yet -- see routes/copilot.py)."""
    missing: dict[str, list[str]] = {"bpm": [], "key": []}
    for f in features:
        if f.bpm is None:
            missing["bpm"].append(f.stable_id)
        if not f.key_camelot:
            missing["key"].append(f.stable_id)
    total = max(1, len(features))
    if any(len(sids) / total > _MISSING_FIELD_LIMIT for sids in missing.values()):
        return missing
    return None


@router.post(
    "/{playlist_id}/solve",
    response_model=PlayItSolveOut,
    responses={
        404: {"model": ErrorBody, "description": "unknown playlist"},
        422: {"model": ErrorBody, "description": "insufficient analysis data / bad goal"},
    },
)
def solve_play_it(
    playlist_id: str,
    body: PlayItGoalIn,
    backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> PlayItSolveOut | JSONResponse:
    # Unknown playlist -> NotFoundError -> 404 via the app-level handler.
    playlist = backend.get_playlist(playlist_id)

    tracks_map = backend.get_tracks_bulk(playlist.items)
    missing_rows = [sid for sid in playlist.items if sid not in tracks_map]
    if missing_rows:
        # Dangling membership = corrupt state.db; fail loudly, mirroring
        # routes/playlists.py's own PLAYLIST_MEMBER_MISSING guard.
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=ErrorBody(
                error="playlist_member_missing",
                message=(
                    f"playlist {playlist_id} references {len(missing_rows)} "
                    f"stable_ids with no track row (first: {missing_rows[:5]})"
                ),
            ).model_dump(),
        )

    ordered_tracks = [tracks_map[sid] for sid in playlist.items]
    features = [_track_to_feature(t) for t in ordered_tracks]

    missing = _missing_fields(features)
    if missing is not None:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=ErrorBody(
                error="insufficient_data",
                message=(
                    f"playlist {playlist_id!r} failed PLAY IT pre-flight: "
                    + "; ".join(
                        f"{field}: {', '.join(sids[:5])}"
                        f"{'...' if len(sids) > 5 else ''}"
                        for field, sids in missing.items()
                        if sids
                    )
                ),
                details={"missing": missing},
            ).model_dump(),
        )

    try:
        goal = SetGoal(
            duration_min=body.duration_min,
            peak_at_min=body.peak_at_min,
            floor_energy=body.floor_energy,
            ceiling_energy=body.ceiling_energy,
            peak_pins=tuple(body.peak_pins),
            opener_pins=tuple(body.opener_pins),
            closer_pin=body.closer_pin,
        )
    except ValueError as exc:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=ErrorBody(error="invalid_goal", message=str(exc)).model_dump(),
        )

    try:
        result = suggest_order(tracks=features, goal=goal)
    except PinUnsatisfiableError as exc:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=ErrorBody(
                error="pin_unsatisfiable",
                message=str(exc),
                details={"reason": exc.reason, "missing": list(exc.missing)},
            ).model_dump(),
        )

    titles = {t.stable_id: t for t in ordered_tracks}
    feature_by_id = {f.stable_id: f for f in features}
    steps = [
        PlayItStepOut(
            position=trace.position,
            stable_id=trace.stable_id,
            title=titles[trace.stable_id].title,
            artist=titles[trace.stable_id].artist,
            bpm=feature_by_id[trace.stable_id].bpm,
            key_camelot=feature_by_id[trace.stable_id].key_camelot,
            energy=feature_by_id[trace.stable_id].energy,
            transition_hint=trace.transition_hint,
            camelot_distance=trace.camelot_distance,
            bpm_delta_pct=trace.bpm_delta_pct,
            target_energy=trace.target_energy,
            actual_energy=trace.actual_energy,
            pin_role=trace.pin_role,
        )
        for trace in result.per_step_trace
    ]

    return PlayItSolveOut(
        playlist_id=playlist.playlist_id,
        etag=compute_etag(playlist.playlist_id, playlist.updated_at),
        previous_order=list(playlist.items),
        proposed_order=list(result.order),
        unchanged=result.order == list(playlist.items),
        steps=steps,
        constraints_unmet=[
            PlayItUnmetOut(kind=u.kind, position=u.position, detail=dict(u.detail))
            for u in result.constraints_unmet
        ],
        solve_ms=result.solve_ms,
    )
