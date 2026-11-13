"""Read and edit playlists: list, tracks, create, rename, add, remove, reorder.

Every tool calls the route the playlist UI calls (``/api/v1/playlists``), so
OJ, the CLI (``opendj api``, ``opendj playlist save-order``) and MCP
(``library``, ``playlist_save_order``) all drive one write path. Reads are gate
``read``; every write is gate ``confirm``.

``playlist_rename`` and ``playlist_reorder`` need ``If-Match``. They read the
playlist's current ETag themselves (``GET .../tracks?limit=1``) unless the
model passes the ``etag`` it got from ``playlist_tracks``. A stale etag is the
engine's 409, reported as ``engine_error`` naming 409 and never retried: the
model re-reads and asks again. Rows come back trimmed to the fields the model
needs (:data:`TRACK_FIELDS`, :data:`PLAYLIST_FIELDS`), read whole through
``engine_json`` so a long playlist is never cut mid-row; the two If-Match
writes answer only the id and the new etag, never the engine's full membership.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlencode

from apps.engine_core.assistant.agent_contract import ToolContext, ToolFailure, ToolSpec
from apps.engine_core.assistant.core_tools import engine_call, engine_json
from apps.engine_core.assistant.oj_tools._args import int_arg, text_arg

# ----- configuration ------------------------------------------------------
PLAYLISTS_PATH: str = "/api/v1/playlists"

#: What one playlist row carries for the model.
PLAYLIST_FIELDS: tuple[str, ...] = ("playlist_id", "name", "vendor", "track_count")
#: What one playlist track row carries; ``item_id`` is what remove and reorder take.
TRACK_FIELDS: tuple[str, ...] = ("item_id", "stable_id", "title", "artist", "bpm", "key", "duration_ms")
#: The fields ``items:save-order`` sorts by (``SaveOrderIn.by``).
SORT_FIELDS: tuple[str, ...] = ("bpm", "key", "title", "artist", "duration", "rating", "genre")
DEFAULT_LIST_LIMIT: int = 100
MAX_LIST_LIMIT: int = 500
DEFAULT_TRACKS_LIMIT: int = 50
#: The tracks route's own bound (``limit`` le=500) is wider; 200 rows stay under the result cap.
MAX_TRACKS_LIMIT: int = 200
MAX_IDS: int = 500


# ----- helpers ------------------------------------------------------------
def _project(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {name: row[name] for name in fields if name in row}


def _playlist_path(args: dict[str, Any], suffix: str = "") -> str:
    playlist_id = text_arg(args, "playlist_id", required=True)
    return f"{PLAYLISTS_PATH}/{quote(str(playlist_id), safe='')}{suffix}"


def _id_list(args: dict[str, Any], name: str, *, required: bool) -> list[str] | None:
    """A non-empty list of non-empty strings (at most MAX_IDS), or None when optional and absent."""
    value = args.get(name)
    if value is None and not required:
        return None
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= MAX_IDS
        or not all(isinstance(item, str) and item.strip() for item in value)
    ):
        raise ToolFailure("invalid_arguments", f"{name} must be a list of 1 to {MAX_IDS} ids, got {value!r}")
    return value


async def _current_etag(ctx: ToolContext, path: str) -> str:
    """The playlist's ETag now, from the cheapest read that carries it."""
    answer = await engine_call(ctx, "GET", f"{path}/tracks?limit=1")
    if answer.etag is None:
        raise ToolFailure("engine_error", f"GET {path}/tracks sent no ETag")
    return answer.etag


async def _if_match(ctx: ToolContext, args: dict[str, Any], path: str) -> str:
    return text_arg(args, "etag") or await _current_etag(ctx, path)


def _playlist_id(args: dict[str, Any]) -> str:
    return str(args["playlist_id"])


# ----- read tools ---------------------------------------------------------
async def _playlist_list(ctx: ToolContext, args: dict[str, Any]) -> Any:
    name = text_arg(args, "name")
    limit = int_arg(args, "limit", low=1, high=MAX_LIST_LIMIT, default=DEFAULT_LIST_LIMIT)
    rows = await engine_json(ctx, "GET", f"{PLAYLISTS_PATH}?availability=skip")
    if name is not None:
        rows = [row for row in rows if name.casefold() in row["name"].casefold()]
    return {"items": [_project(row, PLAYLIST_FIELDS) for row in rows[:limit]], "total": len(rows)}


async def _playlist_tracks(ctx: ToolContext, args: dict[str, Any]) -> Any:
    path = _playlist_path(args)
    limit = int_arg(args, "limit", low=1, high=MAX_TRACKS_LIMIT, default=DEFAULT_TRACKS_LIMIT)
    offset = int_arg(args, "offset", low=0, high=1_000_000, default=0)
    # The etag is read first, so a change between the two reads makes it stale (409), never falsely fresh.
    etag = await _current_etag(ctx, path)
    page = await engine_json(ctx, "GET", f"{path}/tracks?{urlencode({'limit': limit, 'offset': offset})}")
    return {
        "etag": etag,
        "total": page["total"],
        "next_offset": page["next_offset"],
        "items": [_project(row, TRACK_FIELDS) for row in page["tracks"]],
    }


# ----- write tools --------------------------------------------------------
async def _playlist_create(ctx: ToolContext, args: dict[str, Any]) -> Any:
    name = text_arg(args, "name", required=True)
    return _project(await engine_json(ctx, "POST", PLAYLISTS_PATH, {"name": name}), PLAYLIST_FIELDS)


async def _playlist_rename(ctx: ToolContext, args: dict[str, Any]) -> Any:
    path = _playlist_path(args)
    name = text_arg(args, "name", required=True)
    answer = await engine_call(ctx, "PATCH", path, {"name": name}, if_match=await _if_match(ctx, args, path))
    return {"playlist_id": _playlist_id(args), "name": name, "etag": answer.etag}


async def _playlist_add_tracks(ctx: ToolContext, args: dict[str, Any]) -> Any:
    path = _playlist_path(args, "/items:add")
    stable_ids = _id_list(args, "stable_ids", required=True)
    position = int_arg(args, "position", low=0, high=1_000_000)
    body = {"stable_ids": stable_ids, **({"position": position} if position is not None else {})}
    answer = await engine_json(ctx, "POST", path, body)
    added = [{"item_id": row["item_id"], "stable_id": row["stable_id"]} for row in answer["added"]]
    return {**_project(answer, PLAYLIST_FIELDS), "added": added}


async def _playlist_remove_tracks(ctx: ToolContext, args: dict[str, Any]) -> Any:
    path = _playlist_path(args, "/items:remove")
    item_ids = _id_list(args, "item_ids", required=True)
    return _project(await engine_json(ctx, "POST", path, {"item_ids": item_ids}), PLAYLIST_FIELDS)


async def _playlist_reorder(ctx: ToolContext, args: dict[str, Any]) -> Any:
    path = _playlist_path(args)
    by = args.get("by")
    item_ids = _id_list(args, "item_ids", required=False)
    descending = args.get("descending", False)
    if (by is None) == (item_ids is None):
        raise ToolFailure("invalid_arguments", "give exactly one of by or item_ids")
    if by is not None and by not in SORT_FIELDS:
        raise ToolFailure("invalid_arguments", f"by must be one of {', '.join(SORT_FIELDS)}, got {by!r}")
    if not isinstance(descending, bool):
        raise ToolFailure("invalid_arguments", f"descending must be true or false, got {descending!r}")
    body = {"by": by, "descending": descending} if by is not None else {"item_ids": item_ids}
    if_match = await _if_match(ctx, args, path)
    answer = await engine_call(ctx, "POST", f"{path}/items:save-order", body, if_match=if_match)
    return {"playlist_id": _playlist_id(args), "etag": answer.etag}


# ----- specs --------------------------------------------------------------
_STRING = {"type": "string"}
_PLAYLIST_ID = {**_STRING, "description": "The playlist's playlist_id (from playlist_list)."}
_ETAG = {
    **_STRING,
    "description": "Optional etag from playlist_tracks; omit and the tool reads the current one.",
}
_IDS = {"type": "array", "items": _STRING, "minItems": 1, "maxItems": MAX_IDS}
_STALE = "A stale etag fails engine_error naming 409: re-read with playlist_tracks, never retry blind."

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="playlist_list",
        description=(
            "The user's playlists as {items, total}; each item has playlist_id, name, vendor, "
            "track_count. name keeps only playlists whose name contains it (any case). "
            "CLI: opendj api GET /api/v1/playlists; MCP: app_state with that path."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {**_STRING, "description": "Part of a playlist name."},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIST_LIMIT, "default": DEFAULT_LIST_LIMIT},
            },
        },
        gate="read",
        run=_playlist_list,
    ),
    ToolSpec(
        name="playlist_tracks",
        description=(
            "One playlist's tracks in order as {etag, total, next_offset, items}; each item has "
            "item_id (what remove and reorder take), stable_id, title, artist, bpm, key, duration_ms. "
            "An unknown playlist fails engine_error naming 404. "
            "CLI: opendj api GET /api/v1/playlists/{id}/tracks; MCP: app_state with that path."
        ),
        parameters={
            "type": "object",
            "properties": {
                "playlist_id": _PLAYLIST_ID,
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_TRACKS_LIMIT,
                    "default": DEFAULT_TRACKS_LIMIT,
                },
                "offset": {"type": "integer", "minimum": 0, "default": 0},
            },
            "required": ["playlist_id"],
        },
        gate="read",
        run=_playlist_tracks,
    ),
    ToolSpec(
        name="playlist_create",
        description=(
            "Create an empty playlist; answers its playlist_id and name. Add tracks with "
            "playlist_add_tracks. CLI: opendj api POST /api/v1/playlists; MCP: library POST."
        ),
        parameters={
            "type": "object",
            "properties": {"name": {**_STRING, "description": "The new playlist's name."}},
            "required": ["name"],
        },
        gate="confirm",
        run=_playlist_create,
    ),
    ToolSpec(
        name="playlist_rename",
        description=(
            f"Rename a playlist; answers playlist_id, name and the new etag. {_STALE} "
            "CLI: opendj api PATCH /api/v1/playlists/{id} with If-Match; MCP: library PATCH."
        ),
        parameters={
            "type": "object",
            "properties": {
                "playlist_id": _PLAYLIST_ID,
                "name": {**_STRING, "description": "The new name."},
                "etag": _ETAG,
            },
            "required": ["playlist_id", "name"],
        },
        gate="confirm",
        run=_playlist_rename,
    ),
    ToolSpec(
        name="playlist_add_tracks",
        description=(
            "Add tracks by stable_id (from library_search), in the order given, at the end or at "
            "0-based position; answers the new item_ids. "
            "CLI: opendj api POST /api/v1/playlists/{id}/items:add; MCP: library POST."
        ),
        parameters={
            "type": "object",
            "properties": {
                "playlist_id": _PLAYLIST_ID,
                "stable_ids": {**_IDS, "description": "Tracks to add, in order."},
                "position": {"type": "integer", "minimum": 0, "description": "Insert index; omit to append."},
            },
            "required": ["playlist_id", "stable_ids"],
        },
        gate="confirm",
        run=_playlist_add_tracks,
    ),
    ToolSpec(
        name="playlist_remove_tracks",
        description=(
            "Remove entries from a playlist by item_id (from playlist_tracks); the tracks stay in "
            "the library; answers the playlist's new track_count. "
            "CLI: opendj api POST /api/v1/playlists/{id}/items:remove; MCP: library POST."
        ),
        parameters={
            "type": "object",
            "properties": {
                "playlist_id": _PLAYLIST_ID,
                "item_ids": {**_IDS, "description": "Playlist entries to remove."},
            },
            "required": ["playlist_id", "item_ids"],
        },
        gate="confirm",
        run=_playlist_remove_tracks,
    ),
    ToolSpec(
        name="playlist_reorder",
        description=(
            "Make an order the playlist's stored order, one undo step: by a field (untagged last) "
            f"or item_ids (every entry exactly once); answers playlist_id and the new etag. {_STALE} "
            "CLI: opendj playlist save-order; MCP: playlist_save_order."
        ),
        parameters={
            "type": "object",
            "properties": {
                "playlist_id": _PLAYLIST_ID,
                "by": {"type": "string", "enum": list(SORT_FIELDS)},
                "descending": {"type": "boolean", "default": False},
                "item_ids": {**_IDS, "description": "Every entry's item_id in the new order."},
                "etag": _ETAG,
            },
            "required": ["playlist_id"],
        },
        gate="confirm",
        run=_playlist_reorder,
    ),
)
