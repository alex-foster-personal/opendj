"""Global FTS5 collection search endpoint (global-fts5-search node).

GET /api/v1/search searches the WHOLE local collection -- not just the
active browser pane -- across title, artist, genre, comments/notes and
custom tags, ranked by bm25 over a SQLite FTS5 index built off state.db
(see ``search_index.py``; state.db's own schema is never mutated). Hits
are hydrated into the exact playlist-detail row shape
(``rb_vendor.build_track_rows``) plus one added field, ``match_context``,
an excerpt of what matched.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from .. import rb_vendor, search_index
from ..backend import StateBackend
from ..deps import get_library_data_dir, get_read_state
from ..models import TrackRowOut

router = APIRouter(prefix="/search", tags=["search"])

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


class SearchHit(TrackRowOut):
    """One hydrated result row + the FTS match excerpt."""

    match_context: str


class SearchResults(BaseModel):
    query: str
    items: list[SearchHit]
    total: int
    next_offset: int | None = None


def _state_db_path(request: Request) -> Path:
    # Same lookup health.py uses for the same app.state field.
    return Path(getattr(request.app.state, "state_db_path", "data/state/state.db"))


@router.get("", response_model=SearchResults)
def search_collection(
    q: str = Query(
        "",
        description=(
            "Search text, matched against title, artist, genre, comments, notes "
            "and custom tags across the whole collection (not just the "
            "active pane)."
        ),
    ),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    state_db_path: Path = Depends(_state_db_path),  # noqa: B008  # FastAPI DI
    backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
    data_dir: Path = Depends(get_library_data_dir),  # noqa: B008  # FastAPI DI
) -> SearchResults:
    if not q.strip():
        return SearchResults(query=q, items=[], total=0, next_offset=None)

    if "\x00" in q:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "SEARCH_QUERY_INVALID",
                "message": "search query must not contain NUL bytes",
            },
        )

    try:
        hits, total = search_index.search(
            state_db_path, q, limit=limit, offset=offset,
        )
    except search_index.SearchIndexUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "SEARCH_INDEX_UNAVAILABLE", "message": str(exc)},
        ) from exc
    except search_index.SearchQueryError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "SEARCH_QUERY_INVALID", "message": str(exc)},
        ) from exc

    context_by_id = dict(hits)
    stable_ids = [sid for sid, _ in hits]
    tracks_map = backend.get_tracks_bulk(stable_ids)
    # A hit can outlive its track row between an index build and a later
    # delete (real, if rare, race) -- drop it rather than render a
    # half-hydrated row; membership order (bm25 rank) is preserved.
    ordered = [tracks_map[sid] for sid in stable_ids if sid in tracks_map]
    rows = rb_vendor.build_track_rows(ordered, data_dir=data_dir)
    items = [
        SearchHit(**row, match_context=context_by_id.get(row["stable_id"], ""))
        for row in rows
    ]
    next_offset = offset + len(hits) if offset + len(hits) < total else None
    return SearchResults(query=q, items=items, total=total, next_offset=next_offset)


__all__ = ["SearchHit", "SearchResults", "router"]
