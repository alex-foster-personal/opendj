"""Tracks like a given one, and the genres of a set of tracks: ``tracks_similar`` and ``genre_list``.

``tracks_similar`` reads ``GET /api/v1/genres/{stable_id}/similar``: the
duplicate groups whose stored genre vector is closest (cosine) to the track's.
That route answers ids and scores only, so each neighbour is then read with
``GET /api/v1/tracks/{id}`` and cut to :data:`search.ROW_FIELDS` plus its
``score``. A track with no genre vector fails ``engine_error`` naming 404: the
ranking needs genre inference to have run, and this module never starts it.

``genre_list`` reads ``GET /api/v1/genres?ids=...`` (the per-track chosen
genre) and answers the genres those tracks have, each with how many of them
carry it, plus the one chosen genre per track. The engine has no route that
counts genres over the whole library, so the model passes the ids it cares
about (a search page, a playlist, a ``tracks_similar`` answer).

Both only GET; neither calls a model, an embedding service or inference.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from typing import Any
from urllib.parse import quote, urlencode

from apps.engine_core.assistant.agent_contract import ToolContext, ToolFailure, ToolSpec
from apps.engine_core.assistant.core_tools import engine_json
from apps.engine_core.assistant.oj_tools._args import int_arg, text_arg
from apps.engine_core.assistant.oj_tools.search import ROW_FIELDS, TRACKS_PATH

# ----- configuration ------------------------------------------------------
GENRES_PATH: str = "/api/v1/genres"
DEFAULT_LIMIT: int = 20
MAX_LIMIT: int = 50
MAX_GENRE_IDS: int = 50


# ----- helpers ------------------------------------------------------------
def _track_path(stable_id: str) -> str:
    return quote(stable_id, safe="")


def _stable_ids_arg(args: dict[str, Any]) -> list[str]:
    """1 to MAX_GENRE_IDS non-empty strings, duplicates dropped in order; else ``invalid_arguments``."""
    value = args.get("stable_ids")
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= MAX_GENRE_IDS
        or not all(isinstance(sid, str) and sid.strip() for sid in value)
    ):
        raise ToolFailure(
            "invalid_arguments", f"stable_ids must be a list of 1 to {MAX_GENRE_IDS} non-empty strings, got {value!r}"
        )
    return list(dict.fromkeys(value))


async def _row(ctx: ToolContext, stable_id: str, score: float) -> dict[str, Any]:
    track = await engine_json(ctx, "GET", f"{TRACKS_PATH}/{_track_path(stable_id)}")
    return {**{name: track[name] for name in ROW_FIELDS if name in track}, "score": score}


# ----- tools --------------------------------------------------------------
async def _tracks_similar(ctx: ToolContext, args: dict[str, Any]) -> Any:
    stable_id = str(text_arg(args, "stable_id", required=True))
    limit = int_arg(args, "limit", low=1, high=MAX_LIMIT, default=DEFAULT_LIMIT)
    query = urlencode({"limit": limit})
    ranked = await engine_json(ctx, "GET", f"{GENRES_PATH}/{_track_path(stable_id)}/similar?{query}")
    neighbours = [hit for hit in ranked["similar"] if hit["stable_id"] != stable_id]
    rows = await asyncio.gather(*(_row(ctx, hit["stable_id"], hit["score"]) for hit in neighbours))
    return {"stable_id": stable_id, "provider": ranked["provider"], "items": list(rows)}


async def _genre_list(ctx: ToolContext, args: dict[str, Any]) -> Any:
    stable_ids = _stable_ids_arg(args)
    answer = await engine_json(ctx, "GET", f"{GENRES_PATH}?{urlencode({'ids': stable_ids}, doseq=True)}")
    chosen = {sid: (answer["tracks"][sid]["chosen"] or {}).get("genre") for sid in stable_ids}
    counts = Counter(genre for genre in chosen.values() if genre is not None)
    genres = [{"name": name, "count": count} for name, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    tracks = [{"stable_id": sid, "genre": genre} for sid, genre in chosen.items()]
    return {"genres": genres, "untagged": sum(genre is None for genre in chosen.values()), "tracks": tracks}


_STRING = {"type": "string"}

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="genre_list",
        description=(
            "The genres a set of tracks has: give stable_ids (from library_search, a playlist or "
            "tracks_similar). Answers {genres: [{name, count}] most common first, untagged, "
            "tracks: [{stable_id, genre}]}, the genre being the one the Library row shows. "
            "An unknown id fails engine_error naming 404. "
            "CLI: python -m apps.genre_infer findings ID [ID ...]; MCP: genres_get."
        ),
        parameters={
            "type": "object",
            "properties": {
                "stable_ids": {
                    "type": "array",
                    "items": _STRING,
                    "minItems": 1,
                    "maxItems": MAX_GENRE_IDS,
                    "description": "The tracks' stable_ids.",
                }
            },
            "required": ["stable_ids"],
        },
        gate="read",
        run=_genre_list,
    ),
    ToolSpec(
        name="tracks_similar",
        description=(
            "Tracks like one track, by genre profile, closest first; never the track itself or its "
            "duplicates. Answers {stable_id, provider, items}; each item has stable_id, title, "
            "artist, bpm, key, rating, tags, genre, duration_ms and score (cosine, 1 is identical). "
            "A track whose genres were never inferred fails engine_error naming 404. "
            "CLI: python -m apps.genre_infer similar ID --limit N; MCP: genres_similar."
        ),
        parameters={
            "type": "object",
            "properties": {
                "stable_id": {**_STRING, "description": "The track to match, from library_search."},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": DEFAULT_LIMIT},
            },
            "required": ["stable_id"],
        },
        gate="read",
        run=_tracks_similar,
    ),
)
