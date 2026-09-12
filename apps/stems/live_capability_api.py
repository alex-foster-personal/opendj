"""Agent-visible live-stems install capability route."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request, status

from apps.stems.live_capability import (
    LIVE_STEMS_CAPABILITY_PATH,
    LiveStemsCapability,
    LiveStemsCapabilityError,
    plan_live_stems,
)

router = APIRouter(prefix="/stems", tags=["stems"])


def _capability(request: Request) -> LiveStemsCapability:
    capability = getattr(request.app.state, "live_stems_capability", None)
    if not isinstance(capability, LiveStemsCapability):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "live-stems capability is not assessed because this engine is "
                "not running from an installed payload"
            ),
        )
    return capability


@router.get("/live-capability")
def get_live_stems_capability(
    request: Request,
    deck_count: int | None = Query(default=None),
    bpm: float = Query(default=120.0),
) -> dict[str, object]:
    """Read install-time capability and, when requested, its deck plan."""
    capability = _capability(request)
    body = capability.to_dict()
    if deck_count is None:
        return body
    try:
        body["plan"] = plan_live_stems(capability, deck_count=deck_count, bpm=bpm).to_dict()
    except LiveStemsCapabilityError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return body


assert LIVE_STEMS_CAPABILITY_PATH == "/api/v1/stems/live-capability"
