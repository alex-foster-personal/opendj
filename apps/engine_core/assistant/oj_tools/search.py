"""Find tracks and read one: ``library_search`` and ``track_get``.

``library_search`` picks the route from what it is given. ``q`` alone is the
ranked full-text search (``GET /api/v1/search``: title, artist, genre,
comments, notes, tags and lyrics). Any filter (``bpm_min``, ``bpm_max``,
``key``, ``tag``, ``rating_min``) is the filtered track list
(``GET /api/v1/tracks``), where ``q`` narrows to a title or artist substring,
so every filter given is applied by the engine and none by this module. Rows
come back as :data:`ROW_FIELDS` only: a full row is about 3 KB, and the
model needs a dozen tracks, not four.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlencode

from apps.engine_core.assistant.agent_contract import ToolContext, ToolFailure, ToolSpec
from apps.engine_core.assistant.core_tools import engine_call, engine_json
from apps.engine_core.assistant.oj_tools._args import int_arg, number_arg, text_arg

# ----- configuration ------------------------------------------------------
SEARCH_PATH: str = "/api/v1/search"
TRACKS_PATH: str = "/api/v1/tracks"

#: What one search row carries for the model; ``match_context`` only on full-text hits.
ROW_FIELDS: tuple[str, ...] = (
    "stable_id",
    "title",
    "artist",
    "bpm",
    "key",
    "rating",
    "tags",
    "genre",
    "duration_ms",
    "match_context",
)
DEFAULT_LIMIT: int = 20
MAX_LIMIT: int = 50
#: The search route's own BPM bound (``reference_bpm`` le=400).
MAX_BPM: float = 400.0
MAX_RATING: int = 5


# ----- helpers ------------------------------------------------------------
def _compact(row: dict[str, Any]) -> dict[str, Any]:
    return {name: row[name] for name in ROW_FIELDS if name in row}


def _filters(args: dict[str, Any]) -> dict[str, Any]:
    """The /tracks filters given, checked; empty when the call is a plain search."""
    bpm_min = number_arg(args, "bpm_min", low=0.0, high=MAX_BPM)
    bpm_max = number_arg(args, "bpm_max", low=0.0, high=MAX_BPM)
    if bpm_min is not None and bpm_max is not None and bpm_min > bpm_max:
        raise ToolFailure("invalid_arguments", f"bpm_min {bpm_min:g} is above bpm_max {bpm_max:g}")
    given = {
        "bpm_min": bpm_min,
        "bpm_max": bpm_max,
        "key": text_arg(args, "key"),
        "tag": text_arg(args, "tag"),
        "rating_min": int_arg(args, "rating_min", low=0, high=MAX_RATING),
    }
    return {name: value for name, value in given.items() if value is not None}


# ----- tools --------------------------------------------------------------
async def _library_search(ctx: ToolContext, args: dict[str, Any]) -> Any:
    q = text_arg(args, "q")
    limit = int_arg(args, "limit", low=1, high=MAX_LIMIT, default=DEFAULT_LIMIT)
    filters = _filters(args)
    full_text = q is not None and not filters
    query = {**({"q": q} if q is not None else {}), **filters, "limit": limit}
    route = SEARCH_PATH if full_text else TRACKS_PATH
    page = await engine_json(ctx, "GET", f"{route}?{urlencode(query)}")
    more = page["next_offset" if full_text else "next_cursor"] is not None
    return {"route": route, "items": [_compact(row) for row in page["items"]], "more": more}


async def _track_get(ctx: ToolContext, args: dict[str, Any]) -> Any:
    stable_id = text_arg(args, "stable_id", required=True)
    answer = await engine_call(ctx, "GET", f"{TRACKS_PATH}/{quote(str(stable_id), safe='')}")
    return {"etag": answer.etag, "track": answer.body}


_STRING = {"type": "string"}

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="library_search",
        description=(
            "Find tracks in the user's library. q alone is a ranked full-text search over title, "
            "artist, genre, comments, notes, tags and lyrics. Any filter (bpm_min, bpm_max, key, "
            "tag, rating_min) lists tracks matching every filter, with q then matching title or "
            "artist. Answers {route, items, more}; each item has stable_id, title, artist, bpm, "
            "key, rating, tags, genre, duration_ms. Use track_get for one whole track. "
            "CLI: opendj api GET '/api/v1/search?q=...' or '/api/v1/tracks?bpm_min=...'; "
            "MCP: app_state with the same path."
        ),
        parameters={
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "Words to find."},
                "bpm_min": {"type": "number", "minimum": 0, "maximum": MAX_BPM},
                "bpm_max": {"type": "number", "minimum": 0, "maximum": MAX_BPM},
                "key": {"type": "string", "description": "Camelot (8A) or musical (Am) key."},
                "tag": {"type": "string", "description": "One of the user's tags, exactly."},
                "rating_min": {"type": "integer", "minimum": 0, "maximum": MAX_RATING},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": DEFAULT_LIMIT},
            },
        },
        gate="read",
        run=_library_search,
    ),
    ToolSpec(
        name="track_get",
        description=(
            "One track's whole row by stable_id (from library_search), with its etag for "
            "library_write's if_match. An unknown id fails engine_error naming 404. "
            "CLI: opendj api GET /api/v1/tracks/{stable_id}; MCP: app_state with that path."
        ),
        parameters={
            "type": "object",
            "properties": {"stable_id": {**_STRING, "description": "The track's stable_id."}},
            "required": ["stable_id"],
        },
        gate="read",
        run=_track_get,
    ),
)
