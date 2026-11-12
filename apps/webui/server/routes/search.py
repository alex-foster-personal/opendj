"""Global FTS5 collection search endpoint (global-fts5-search node).

GET /api/v1/search searches the WHOLE local collection -- not just the
active browser pane -- across title, artist, genre, comments/notes and
custom tags, ranked by bm25 over a SQLite FTS5 index built off state.db
(see ``search_index.py``; state.db's own schema is never mutated). Hits
are hydrated into the exact playlist-detail row shape
(``rb_vendor.build_track_rows``) plus one added field, ``match_context``,
an excerpt of what matched.

Lyric also-search (issue #11146) merges indexed lyric FTS/fuzzy hits into the
same ranked list via ``hybrid_rank.py`` (metadata vs lyrics provenance), never
by concatenating two API responses.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from apps.lyrics.search_snippet import matched_snippet

from .. import rb_vendor, search_index
from ..backend import StateBackend
from ..deps import get_library_data_dir, get_library_state_db, get_read_state
from ..hybrid_rank import (
    DeckReference,
    HybridCandidate,
    TieBreakFields,
    lyric_band,
    merge_hybrid_candidates,
    metadata_band,
)
from ..models import TrackRowOut
from ..shell_output_health import lyric_index_data_dir_from_state_db
from ..lyric_search_collect import collect_lyric_hit_ids

router = APIRouter(prefix="/search", tags=["search"])
MOUNT_UNDER_API_PREFIX = True

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
_HYBRID_CANDIDATE_CAP = 500


class SearchHit(TrackRowOut):
    """One hydrated result row + the FTS match excerpt."""

    match_context: str
    match_provenance: Literal["metadata", "lyrics", "ai_library", "ai_external"] = Field(
        default="metadata",
        description="Whether the winning match came from metadata FTS or lyric index.",
    )


class SearchResults(BaseModel):
    query: str
    items: list[SearchHit]
    total: int
    next_offset: int | None = None


def _state_db_path(request: Request) -> Path:
    # Same lookup health.py uses for the same app.state field.
    return Path(getattr(request.app.state, "state_db_path", "(private path)"))


def _hybrid_candidate_cap(limit: int, offset: int) -> int:
    return min(_HYBRID_CANDIDATE_CAP, max(200, limit + offset + 50))


def _tie_fields(row: dict[str, object], track_updated_at: str | None) -> TieBreakFields:
    bpm_raw = row.get("bpm")
    bpm = float(bpm_raw) if isinstance(bpm_raw, (int, float)) else None
    key_raw = row.get("key")
    key = str(key_raw) if key_raw is not None else None
    play_raw = row.get("play_count", 0)
    play_count = int(play_raw) if isinstance(play_raw, int) else 0
    last_played = row.get("last_played_at")
    return TieBreakFields(
        play_count=play_count,
        last_played_at=str(last_played) if last_played is not None else None,
        modified_at=track_updated_at,
        bpm=bpm,
        key=key,
    )


def _build_hybrid_candidates(
    query: str,
    data_dir: Path,
    meta_hits: list[search_index.MetadataHit],
    lyric_fts: frozenset[str],
    lyric_fuzzy: frozenset[str],
    rows_by_id: dict[str, dict[str, object]],
    tracks_updated_at: dict[str, str | None],
) -> list[HybridCandidate]:
    candidates: list[HybridCandidate] = []
    for hit in meta_hits:
        row = rows_by_id.get(hit.stable_id)
        if row is None:
            continue
        candidates.append(
            HybridCandidate(
                stable_id=hit.stable_id,
                match_context=hit.match_context,
                provenance="metadata",
                band=metadata_band(query, hit.title, hit.artist),
                tie=_tie_fields(row, tracks_updated_at.get(hit.stable_id)),
            )
        )
    lyric_ids = set(lyric_fts) | set(lyric_fuzzy)
    for stable_id in lyric_ids:
        row = rows_by_id.get(stable_id)
        if row is None:
            continue
        snippet = matched_snippet(data_dir, stable_id, query)
        if snippet is None:
            continue
        candidates.append(
            HybridCandidate(
                stable_id=stable_id,
                match_context=snippet,
                provenance="lyrics",
                band=lyric_band(fts_hit=stable_id in lyric_fts),
                tie=_tie_fields(row, tracks_updated_at.get(stable_id)),
            )
        )
    return candidates


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
    reference_bpm: float | None = Query(
        None,
        ge=0,
        le=400,
        description="Optional loaded-deck BPM for hybrid tie-break proximity.",
    ),
    reference_key: str | None = Query(
        None,
        max_length=20,
        description="Optional loaded-deck key for hybrid tie-break proximity.",
    ),
    state_db_path: Path = Depends(_state_db_path),  # noqa: B008  # FastAPI DI
    backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
    data_dir: Path = Depends(get_library_data_dir),  # noqa: B008  # FastAPI DI
    state_db: Path = Depends(get_library_state_db),  # noqa: B008  # FastAPI DI
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

    cap = _hybrid_candidate_cap(limit, offset)
    lyric_data_dir = lyric_index_data_dir_from_state_db(state_db_path)
    deck = (
        DeckReference(bpm=reference_bpm, key=reference_key)
        if reference_bpm is not None or reference_key is not None
        else None
    )

    try:
        meta_hits, meta_total = search_index.search_metadata_hits(
            state_db_path, q, cap=cap,
        )
        lyric_fts, lyric_fuzzy, lyric_total = collect_lyric_hit_ids(
            lyric_data_dir, q, cap=cap,
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

    candidate_ids = list(
        dict.fromkeys(
            [h.stable_id for h in meta_hits]
            + list(lyric_fts)
            + list(lyric_fuzzy)
        )
    )
    if not candidate_ids:
        return SearchResults(query=q, items=[], total=0, next_offset=None)

    tracks_map = backend.get_tracks_bulk(candidate_ids)
    ordered_tracks = [tracks_map[sid] for sid in candidate_ids if sid in tracks_map]
    rows = rb_vendor.build_track_rows(ordered_tracks, data_dir=data_dir, state_db_path=state_db)
    rows_by_id = {row["stable_id"]: row for row in rows}
    tracks_updated_at = {sid: tracks_map[sid].updated_at for sid in tracks_map}

    candidates = _build_hybrid_candidates(
        q,
        lyric_data_dir,
        meta_hits,
        lyric_fts,
        lyric_fuzzy,
        rows_by_id,
        tracks_updated_at,
    )
    ranked = merge_hybrid_candidates(candidates, deck=deck)

    if meta_total <= cap and lyric_total <= cap:
        total = len(ranked)
    else:
        total = max(meta_total, lyric_total)

    page = ranked[offset : offset + limit]
    items = [
        SearchHit(
            **rows_by_id[candidate.stable_id],
            match_context=candidate.match_context,
            match_provenance=candidate.provenance,
        )
        for candidate in page
        if candidate.stable_id in rows_by_id
    ]
    next_offset = offset + len(page) if offset + len(page) < total else None
    return SearchResults(query=q, items=items, total=total, next_offset=next_offset)


__all__ = ["SearchHit", "SearchResults", "router"]
