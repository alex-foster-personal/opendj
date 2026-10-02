"""Lyric-only search endpoint (Part 3 of #935, issue #1344).

GET /api/v1/lyrics/search queries the on-disk lyric-search index
(``apps.lyrics.search_index``, Part 2 of #935) built over cached synced
lyrics, and hydrates each hit into the exact same envelope the metadata
search returns (``routes/search.py``'s ``SearchHit``/``SearchResults``): a
hit is a hydrated track row plus an excerpt of what matched, regardless of
whether the match was found in metadata or in a cached lyric. Reusing that
shape rather than declaring a near-identical sibling keeps one schema in
``openapi.json`` instead of two. Here, ``match_context`` is the one lyric
line that best carries the query (``apps.lyrics.search_snippet``) - a phrase
in context, never the bare title or the whole transcript. This route only
answers "what matched and where"; rendering it below the metadata search,
after a divider, lazily and debounced, is the frontend's job
(BrowserPanel.svelte + LyricSearchResults.svelte).
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request

from apps.lyrics.search_index import DEFAULT_SEARCH_LIMIT, MAX_SEARCH_LIMIT, search_documents
from apps.lyrics.search_snippet import matched_snippet

from .. import rb_vendor
from ..backend import StateBackend
from ..deps import get_read_state
from .search import SearchHit, SearchResults

router = APIRouter(prefix="/lyrics/search", tags=["lyrics"])


def _data_dir(request: Request) -> Path:
    # Same derivation apps.webui.server.app.build_lyric_index_watcher uses to
    # bind the background indexer to this app's data (state_db_path is
    # data_dir/state/state.db).
    state_db_path = Path(getattr(request.app.state, "state_db_path", "data/state/state.db"))
    return state_db_path.parent.parent


@router.get("", response_model=SearchResults)
def search_lyrics(
    q: str = Query(
        "",
        description=(
            "Search text matched against cached synced lyrics only - never "
            "title, artist, genre or tags (see GET /api/v1/search for that)."
        ),
    ),
    limit: int = Query(DEFAULT_SEARCH_LIMIT, ge=1, le=MAX_SEARCH_LIMIT),
    offset: int = Query(0, ge=0),
    data_dir: Path = Depends(_data_dir),  # noqa: B008
    backend: StateBackend = Depends(get_read_state),  # noqa: B008
) -> SearchResults:
    if not q.strip():
        return SearchResults(query=q, items=[], total=0, next_offset=None)

    stable_ids, total = search_documents(data_dir, q, limit=limit, offset=offset)
    if not stable_ids:
        return SearchResults(query=q, items=[], total=total, next_offset=None)

    tracks_map = backend.get_tracks_bulk(stable_ids)
    rows_by_id = {
        row["stable_id"]: row
        for row in rb_vendor.build_track_rows(
            [tracks_map[sid] for sid in stable_ids if sid in tracks_map], data_dir=data_dir
        )
    }
    items: list[SearchHit] = []
    for stable_id in stable_ids:
        row = rows_by_id.get(stable_id)
        if row is None:
            # A hit can outlive its track row between an index build and a
            # later delete (real, if rare, race) -- drop it rather than
            # render a half-hydrated row, same convention as routes/search.py.
            continue
        snippet = matched_snippet(data_dir, stable_id, q)
        if snippet is None:
            continue
        items.append(SearchHit(**row, match_context=snippet))

    next_offset = offset + len(stable_ids) if offset + len(stable_ids) < total else None
    return SearchResults(query=q, items=items, total=total, next_offset=next_offset)


__all__ = ["router"]
