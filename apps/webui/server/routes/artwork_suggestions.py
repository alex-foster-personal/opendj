"""Store cover suggestions waiting on the user's consent (LIBM-188, LIBM-189).

When MusicBrainz has no cover and Deezer or iTunes has the track, the lookup
records a SUGGESTION (the cover's URL and names), never the image: their terms
do not allow caching their content. The user (or an agent acting for them)
decides per track:

  GET  /artwork/suggestions                 pending suggestions + the consent note
  GET  /artwork/suggestions/{id}/image      the suggested cover, fetched live for the
                                            preview and never stored (no-store)
  POST /artwork/suggestions/decide          {"accept": [ids], "skip": [ids]} or
                                            {"accept_all": true} / {"skip_all": true}

Accept fetches and caches the cover; skip is remembered so that track is never
suggested a store cover again. CLI twin: ``opendj artwork``; MCP:
``artwork_suggestions_list`` / ``artwork_suggestions_decide``.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from apps.shared import artwork_sources

router = APIRouter(prefix="/artwork/suggestions", tags=["artwork"])
MOUNT_UNDER_API_PREFIX = True

CONSENT_NOTE: str = (
    "* Deezer and iTunes terms do not allow caching their content; only save covers "
    "for tracks you own and bought on their platform."
)
SOURCE_LABELS: dict[str, str] = {"deezer": "Deezer", "itunes": "iTunes"}


def _cache() -> artwork_sources.ArtworkCache:
    from apps.adapters.rekordbox import paths

    return paths.online_artwork_cache()


def pending_document() -> dict[str, Any]:
    pending = _cache().pending_suggestions()
    return {
        "note": CONSENT_NOTE,
        "pending": [
            {
                "stable_id": sid,
                **asdict(s),
                "source_label": SOURCE_LABELS.get(s.source, s.source),
                "image_url": f"/api/v1/artwork/suggestions/{sid}/image",
            }
            for sid, s in pending.items()
        ],
    }


class ArtworkSuggestionDecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accept: list[str] = Field(default_factory=list, max_length=5000)
    skip: list[str] = Field(default_factory=list, max_length=5000)
    accept_all: bool = False
    skip_all: bool = False


@router.get("")
def get_suggestions() -> dict[str, Any]:
    return pending_document()


@router.get("/{stable_id}/image")
def get_suggestion_image(stable_id: str) -> Response:
    suggestion = _cache().suggestion(stable_id)
    if suggestion is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "ARTWORK_SUGGESTION_NOT_FOUND", "message": f"no pending cover suggestion for {stable_id}"},
        )
    try:
        data, mime = artwork_sources.fetch_suggested_cover(suggestion)
    except artwork_sources.SuggestionError as exc:
        raise HTTPException(status_code=502, detail={"code": "ARTWORK_SUGGESTION_FETCH", "message": str(exc)}) from exc
    return Response(content=data, media_type=mime, headers={"Cache-Control": "no-store"})


@router.post("/decide")
def post_decide(body: ArtworkSuggestionDecisionIn) -> dict[str, Any]:
    cache = _cache()
    pending = list(cache.pending_suggestions())
    accept = pending if body.accept_all else body.accept
    skip = [sid for sid in (pending if body.skip_all else body.skip) if sid not in set(accept)]
    if not accept and not skip:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "ARTWORK_DECIDE_EMPTY",
                "message": "name tracks to accept or skip, or pass accept_all / skip_all",
            },
        )
    results: dict[str, dict[str, str]] = {}
    for sid in accept:
        try:
            suggestion = artwork_sources.accept_suggestion(cache, sid)
            results[sid] = {"decision": "accepted", "source": suggestion.source}
        except artwork_sources.SuggestionError as exc:
            results[sid] = {"decision": "error", "reason": str(exc)}
    for sid in skip:
        try:
            suggestion = artwork_sources.skip_suggestion(cache, sid)
            results[sid] = {"decision": "skipped", "source": suggestion.source}
        except artwork_sources.SuggestionError as exc:
            results[sid] = {"decision": "error", "reason": str(exc)}
    return {"results": results, **pending_document()}
