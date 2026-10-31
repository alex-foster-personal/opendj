"""Cmd K AI search (CMDK-20, issue #6093).

GET  /api/v1/cmdk/ai-status  -- whether an OpenRouter key is set, and the model.
POST /api/v1/cmdk/ai-search  -- rank the bar's shortlist against the DJ's words
                                with ONE JEV call (:mod:`apps.genre_infer.jev_search`).

The key is read from the engine's environment (``OPENROUTER_API_KEY``), never
sent to the browser, logged or stored. With ``cmdk.v2`` or ``cmdk.v2.ai``
off, ai-search is a 403 with code ``ai_search_disabled`` before anything is
sent upstream, so a direct HTTP or CLI caller cannot spend OpenRouter credit
while the rollout gate is closed. No key is a 409 with code
``ai_search_key_missing``; a failed or unusable answer is a 502 with code
``ai_search_upstream_error``. None returns a ranking.

JEV is sent only library-held metadata: every submitted ``stable_id`` is
resolved against the engine's own library, and the fields sent upstream are
read from it, never taken from the request. An id the library does not hold
is a 422 with code ``ai_search_unknown_track`` and nothing is sent, so the
engine's key is never a relay for arbitrary text under an invented id.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from apps.genre_infer import jev, jev_search

from ..backend import StateBackend, Track
from ..deps import get_read_state

router = APIRouter(prefix="/cmdk", tags=["cmdk"])

KEY_MISSING_CODE = "ai_search_key_missing"
DISABLED_CODE = "ai_search_disabled"
#: Both must be on: the v2 bar, and AI search inside it.
AI_FLAGS: tuple[str, ...] = ("cmdk.v2", "cmdk.v2.ai")
UPSTREAM_ERROR_CODE = "ai_search_upstream_error"
UNKNOWN_TRACK_CODE = "ai_search_unknown_track"
#: The library fields JEV is sent for each track; the request's own copies are never forwarded.
SENT_FIELDS: tuple[str, ...] = ("title", "artist", "album", "genre", "bpm", "key")


class CmdkAiTrackIn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stable_id: str = Field(min_length=1, max_length=200)
    title: str | None = Field(default=None, max_length=400)
    artist: str | None = Field(default=None, max_length=400)
    album: str | None = Field(default=None, max_length=400)
    genre: str | None = Field(default=None, max_length=200)
    bpm: float | None = Field(default=None, ge=0, le=400)
    key: str | None = Field(default=None, max_length=20)


class CmdkAiSearchIn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    words: str = Field(min_length=1, max_length=jev_search.MAX_WORDS)
    tracks: list[CmdkAiTrackIn] = Field(min_length=1, max_length=jev_search.MAX_TRACKS)


class CmdkAiSearchOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    #: P(the track fits the words) per stable_id, every shortlisted track present.
    scores: dict[str, float]
    model: str | None
    cost: float | None


class CmdkAiStatusOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    configured: bool
    model: str


@router.get("/ai-status", response_model=CmdkAiStatusOut)
def ai_status() -> CmdkAiStatusOut:
    return CmdkAiStatusOut(configured=jev_search.configured(), model=jev.resolve_model())


#: The one call that leaves the engine: the decisions-API POST.
JevPost = Callable[[dict[str, Any], str], dict[str, Any]]


def get_jev_post() -> JevPost:
    """The HTTP call ai-search makes, as a dependency.

    Production always gets ``jev._http_post``. Tests override this dependency to
    serve the reply captured from the live API through the same rank, parse and
    route code, rather than patching module attributes.
    """
    return jev._http_post


def _flags_off(request: Request) -> list[str]:
    """The AI flags that are off, read from the boot-time store.

    Fails fast rather than reading "on" when no store is mounted:
    ``apps.webui.server.app.create_app`` always wires one.
    """
    store = getattr(request.app.state, "feature_flags", None)
    if store is None:
        raise RuntimeError("feature flags are not mounted on app.state.feature_flags; ai-search cannot be gated")
    return [flag for flag in AI_FLAGS if not store.enabled(flag)]


@router.post(
    "/ai-search",
    response_model=CmdkAiSearchOut,
    responses={
        403: {"description": "cmdk.v2 or cmdk.v2.ai is off."},
        409: {"description": f"No {jev.KEY_ENV} is configured."},
        502: {"description": "JEV failed or answered without every track."},
    },
)
def ai_search(
    body: CmdkAiSearchIn,
    request: Request,
    backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
    post: JevPost = Depends(get_jev_post),  # noqa: B008  # FastAPI DI
) -> CmdkAiSearchOut:
    off = _flags_off(request)
    if off:
        raise HTTPException(403, {"code": DISABLED_CODE, "message": f"AI search is off: turn on {', '.join(off)}"})
    if len({t.stable_id for t in body.tracks}) != len(body.tracks):
        raise HTTPException(422, {"code": "ai_search_duplicate_track", "message": "each track may appear once"})
    # Live tracks only: one the DJ removed from the library is not sent anywhere (Codex r4278647456).
    held = backend.get_tracks_bulk([t.stable_id for t in body.tracks], live_only=True)
    unknown = [t.stable_id for t in body.tracks if t.stable_id not in held]
    if unknown:
        raise HTTPException(
            422,
            {"code": UNKNOWN_TRACK_CODE, "message": f"the library holds no track {', '.join(unknown[:5])}"},
        )
    tracks = [jev_search.SearchTrack(t.stable_id, _library_fields(held[t.stable_id])) for t in body.tracks]
    try:
        out: dict[str, Any] = jev_search.rank(body.words, tracks, post=post)
    except jev_search.SearchKeyMissing as exc:
        raise HTTPException(409, {"code": KEY_MISSING_CODE, "message": str(exc)}) from exc
    except jev_search.SearchFailed as exc:
        raise HTTPException(502, {"code": UPSTREAM_ERROR_CODE, "message": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(422, {"code": "ai_search_invalid", "message": str(exc)}) from exc
    return CmdkAiSearchOut(scores=out["scores"], model=out["model"], cost=out["cost"])


def _library_fields(track: Track) -> dict[str, Any]:
    """What JEV is told about one track: the library's own fields, unset ones left out."""
    return {name: value for name in SENT_FIELDS if (value := getattr(track, name)) is not None}
