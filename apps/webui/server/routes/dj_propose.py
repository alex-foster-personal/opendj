"""Offline propose surface (issue #10150): the same document as ``propose_transition`` and the CLI/MCP propose tools."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from apps.autodj.http import ProfileError, TrackProfile, build_service, propose_transition
from apps.shared.engine_origin import EngineOrigin
from apps.transition_score.http import PairNotTryable

from ..deps import get_library_data_dir, get_library_state_db
from ..errors import ErrorBody
from .dj_try import ServingTrackSource, TrackUnavailable, _serving_origin

router = APIRouter(prefix="/dj", tags=["dj-propose"])
MOUNT_UNDER_API_PREFIX = True


class ProposeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    out_stable_id: str
    in_stable_id: str
    out_deck: int = Field(default=1, ge=1, le=4)
    in_deck: int = Field(default=2, ge=1, le=4)
    intent: str | None = None
    top: int = Field(default=3, ge=1, le=50)


def _profiles(
    body: ProposeIn,
    data_dir: Path,
    state_db: Path,
    origin: EngineOrigin,
) -> tuple[TrackProfile, TrackProfile]:
    autodj = build_service()
    source = ServingTrackSource(data_dir, autodj, origin=origin, state_db=state_db)
    try:
        return source.profile(body.out_stable_id), source.profile(body.in_stable_id)
    except (ProfileError, PairNotTryable) as error:
        raise TrackUnavailable(str(error)) from error


@router.post("/propose", responses={422: {"model": ErrorBody}})
def propose_pair(
    request: Request,
    body: ProposeIn,
    data_dir: Annotated[Path, Depends(get_library_data_dir)],
    state_db: Annotated[Path, Depends(get_library_state_db)],
) -> JSONResponse:
    try:
        out_profile, in_profile = _profiles(body, data_dir, state_db, _serving_origin(request, state_db))
    except TrackUnavailable as error:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=ErrorBody(error="track_unavailable", message=str(error)).model_dump(),
        )
    document: dict[str, Any] = propose_transition(
        out_profile,
        in_profile,
        out_deck=body.out_deck,
        in_deck=body.in_deck,
        intent=body.intent,
        top=body.top,
    )
    document.pop("_valid_proposal_scores", None)
    return JSONResponse(content=document)


def propose_parity_document(body: ProposeIn, data_dir: Path, state_db: Path, origin: EngineOrigin) -> bytes:
    from apps.autodj.http import propose_parity_bytes

    out_profile, in_profile = _profiles(body, data_dir, state_db, origin)
    document = propose_transition(
        out_profile,
        in_profile,
        out_deck=body.out_deck,
        in_deck=body.in_deck,
        intent=body.intent,
        top=body.top,
    )
    document.pop("_valid_proposal_scores", None)
    return propose_parity_bytes(document)
