"""The user's My Tags: ``tag_list``, ``tag_create``, ``tag_assign`` and ``tag_unassign``.

``tag_list`` is ``GET /api/v1/mytags`` (every tag a track carries, with its
track count). The two writes are the ETag-checked ``POST /api/v1/mytags/assign``,
which wants each track's current ETag in ``expected_etags``: the tool reads
each track (``GET /api/v1/tracks/{id}``) for its ETag and tags, then sends the
one write, so a track edited in between makes the engine answer 409 and
nothing is written. ``tag_unassign`` refuses before writing when a track does
not carry the tag, so "remove warmup" never reports success for a tag that
was never there. Tags match exactly as the engine stores them.

``tag_create`` is ``POST /api/v1/mytags``: it declares a tag (optionally of a
tag type) with no tracks yet. It lands on OJ's undo session, which every
``engine_call`` write sends, so ``POST /api/v1/undo`` with that session takes
it back. A name that already exists as another type is the engine's 409.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from apps.engine_core.assistant.agent_contract import ToolContext, ToolFailure, ToolSpec
from apps.engine_core.assistant.core_tools import engine_call, engine_json
from apps.engine_core.assistant.oj_tools._args import text_arg

# ----- configuration ------------------------------------------------------
MYTAGS_PATH: str = "/api/v1/mytags"
ASSIGN_PATH: str = "/api/v1/mytags/assign"
TRACKS_PATH: str = "/api/v1/tracks"
#: The assign route's own batch cap (``MyTagAssignIn.stable_ids`` max_length).
MAX_TRACKS: int = 100


# ----- helpers ------------------------------------------------------------
def _stable_ids(args: dict[str, Any]) -> list[str]:
    """1 to MAX_TRACKS distinct non-empty ids, or invalid_arguments."""
    value = args.get("stable_ids")
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= MAX_TRACKS
        or not all(isinstance(item, str) and item.strip() for item in value)
    ):
        raise ToolFailure(
            "invalid_arguments", f"stable_ids must be a list of 1 to {MAX_TRACKS} track ids, got {value!r}"
        )
    if len(set(value)) != len(value):
        raise ToolFailure("invalid_arguments", f"stable_ids repeats an id: {value!r}")
    return value


async def _current(ctx: ToolContext, stable_ids: list[str]) -> dict[str, tuple[str, list[str]]]:
    """Each track's ETag and tags as the engine holds them now; an unknown id is engine_error naming 404."""
    current: dict[str, tuple[str, list[str]]] = {}
    for stable_id in stable_ids:
        path = f"{TRACKS_PATH}/{quote(stable_id, safe='')}"
        answer = await engine_call(ctx, "GET", path)
        if answer.etag is None or not isinstance(answer.body, dict) or not isinstance(answer.body.get("tags"), list):
            raise ToolFailure("engine_error", f"GET {path} answered without an ETag and a tags list")
        current[stable_id] = (answer.etag, answer.body["tags"])
    return current


async def _assign(ctx: ToolContext, args: dict[str, Any], *, add: bool) -> Any:
    stable_ids = _stable_ids(args)
    tag = str(text_arg(args, "tag", required=True)).strip()
    current = await _current(ctx, stable_ids)
    if not add:
        lacking = [stable_id for stable_id, (_etag, tags) in current.items() if tag not in tags]
        if lacking:
            raise ToolFailure("invalid_arguments", f"tag {tag!r} is not on {', '.join(lacking)}; nothing was changed")
    body = {
        "stable_ids": stable_ids,
        "expected_etags": {stable_id: etag for stable_id, (etag, _tags) in current.items()},
        "add" if add else "remove": [tag],
    }
    answer = await engine_json(ctx, "POST", ASSIGN_PATH, body)
    tracks = [{"stable_id": row["stable_id"], "tags": row["tags"]} for row in answer["results"]]
    return {"tag": tag, "applied_count": answer["applied_count"], "tracks": tracks}


# ----- tools --------------------------------------------------------------
async def _tag_create(ctx: ToolContext, args: dict[str, Any]) -> Any:
    body = {"name": str(text_arg(args, "name", required=True)).strip()}
    tag_type = text_arg(args, "type")
    if tag_type is not None:
        body["type"] = tag_type
    answer = await engine_json(ctx, "POST", MYTAGS_PATH, body)
    row = answer["row"]
    return {"name": row["display_name"], "type": row["type"], "undo_id": answer["undo_id"]}


async def _tag_list(ctx: ToolContext, _args: dict[str, Any]) -> Any:
    answer = await engine_json(ctx, "GET", MYTAGS_PATH)
    return {"tags": [{"name": row["name"], "track_count": row["track_count"]} for row in answer["tags"]]}


async def _tag_assign(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return await _assign(ctx, args, add=True)


async def _tag_unassign(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return await _assign(ctx, args, add=False)


_WRITE_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "stable_ids": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": MAX_TRACKS,
            "description": "The tracks' stable_ids (from library_search), each once.",
        },
        "tag": {"type": "string", "description": "One My Tag, exactly as tag_list spells it."},
    },
    "required": ["stable_ids", "tag"],
}

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="tag_assign",
        description=(
            "Add one My Tag to up to 100 tracks; their other tags stay. A new name creates the tag. "
            "Answers {tag, applied_count, tracks: [{stable_id, tags}]}. An unknown id fails "
            "engine_error naming 404; a track edited meanwhile fails naming 409. Nothing is written "
            "on any failure. CLI: opendj api POST /api/v1/mytags/assign; MCP: library with that path."
        ),
        parameters=_WRITE_PARAMETERS,
        gate="confirm",
        run=_tag_assign,
    ),
    ToolSpec(
        name="tag_create",
        description=(
            "Declare one My Tag with no tracks yet, of a tag type (ids from GET /api/v1/tag-types; "
            "omit type for the engine's general tags). Answers {name, type, undo_id}. A name that "
            "exists as another type fails engine_error naming 409. Undo with library_write POST "
            "/api/v1/undo. CLI: opendj api POST /api/v1/mytags -H X-Undo-Session:<id>; MCP: library "
            "with that path."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The tag's name, as the user spells it."},
                "type": {"type": "string", "description": "A tag type id; omitted means general."},
            },
            "required": ["name"],
        },
        gate="confirm",
        run=_tag_create,
    ),
    ToolSpec(
        name="tag_list",
        description=(
            "Every My Tag the user's tracks carry, with how many tracks carry it: {tags: [{name, "
            "track_count}]}. Use library_search with tag= to list one tag's tracks. "
            "CLI: opendj api GET /api/v1/mytags (rename and delete: opendj track mytag-rename, "
            "mytag-delete); MCP: app_state with that path."
        ),
        parameters={"type": "object", "properties": {}},
        gate="read",
        run=_tag_list,
    ),
    ToolSpec(
        name="tag_unassign",
        description=(
            "Remove one My Tag from up to 100 tracks; their other tags stay. Fails invalid_arguments, "
            "writing nothing, when any track does not carry the tag. Answers {tag, applied_count, "
            "tracks: [{stable_id, tags}]}. CLI: opendj api POST /api/v1/mytags/assign; MCP: library "
            "with that path."
        ),
        parameters=_WRITE_PARAMETERS,
        gate="confirm",
        run=_tag_unassign,
    ),
)
