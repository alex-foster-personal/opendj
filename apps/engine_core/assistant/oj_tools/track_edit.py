"""Edit one track or many: ``track_update`` and ``tracks_bulk_edit``.

Both write through the routes the track editor and the bulk editor use, so a
change from OJ lands exactly as one from the UI: ``PATCH /api/v1/tracks/{id}``
under ``If-Match`` for one track, ``PATCH /api/v1/bulk-edit`` with one
``expected_etags`` entry per track for many. Both store to the app's own
state database only (rekordbox and the audio files are not written).

The model never has to fetch an etag: a tool reads each track's ETag itself
unless it was handed one (``track_get`` answers ``{etag, track}``). A stale
etag is the engine's 409, surfaced as ``engine_error``, and the bulk route
checks every row before writing any, so a conflict writes nothing.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from apps.engine_core.assistant.agent_contract import ToolContext, ToolFailure, ToolSpec
from apps.engine_core.assistant.core_tools import engine_call
from apps.engine_core.assistant.oj_tools._args import int_arg, text_arg

# ----- configuration ------------------------------------------------------
TRACKS_PATH: str = "/api/v1/tracks"
BULK_EDIT_PATH: str = "/api/v1/bulk-edit"
#: The bulk route's own cap (``routes/bulk_edit.py`` ``_MAX_STABLE_IDS``).
MAX_STABLE_IDS: int = 100
MAX_RATING: int = 5
TEXT_FIELDS: tuple[str, ...] = ("genre", "comments", "notes")
TAG_FIELDS: tuple[str, ...] = ("tags_add", "tags_remove")
#: What ``track_update`` hands back of the updated row (a whole row is about 3 KB). The
#: single-track row carries no genre (only list rows do): ``library_search`` shows it.
ROW_FIELDS: tuple[str, ...] = ("stable_id", "title", "artist", "rating", "comments", "notes", "tags")


# ----- helpers ------------------------------------------------------------
def _text_list(args: dict[str, Any], name: str, *, high: int) -> list[str] | None:
    """An optional list of 1..high distinct non-empty strings, or None when absent."""
    value = args.get(name)
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= high
        or not all(isinstance(item, str) and item.strip() for item in value)
        or len(set(value)) != len(value)
    ):
        raise ToolFailure(
            "invalid_arguments", f"{name} must be a list of 1 to {high} distinct non-empty strings, got {value!r}"
        )
    return value


def _edit_fields(args: dict[str, Any]) -> dict[str, Any]:
    """The edit fields given, checked; fails ``invalid_arguments`` when there are none."""
    given: dict[str, Any] = {"rating": int_arg(args, "rating", low=0, high=MAX_RATING)}
    given.update({name: text_arg(args, name) for name in TEXT_FIELDS})
    given.update({name: _text_list(args, name, high=MAX_STABLE_IDS) for name in TAG_FIELDS})
    fields = {name: value for name, value in given.items() if value is not None}
    if not fields:
        names = ", ".join(("rating", *TEXT_FIELDS, *TAG_FIELDS))
        raise ToolFailure("invalid_arguments", f"give at least one field to change: {names}")
    return fields


def _track_path(stable_id: str) -> str:
    return f"{TRACKS_PATH}/{quote(stable_id, safe='')}"


async def _current_etag(ctx: ToolContext, stable_id: str) -> str:
    """The track's live ETag, read the way ``track_get`` reads it."""
    etag = (await engine_call(ctx, "GET", _track_path(stable_id))).etag
    if etag is None:
        raise ToolFailure("engine_error", f"GET {_track_path(stable_id)} answered no ETag")
    return etag


def _given_etags(args: dict[str, Any], stable_ids: list[str]) -> dict[str, str]:
    """The optional ``etags`` map, checked to name only tracks in ``stable_ids``."""
    value = args.get("etags")
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(isinstance(etag, str) and etag for etag in value.values()):
        raise ToolFailure("invalid_arguments", f"etags must map stable_id to a non-empty etag, got {value!r}")
    strangers = sorted(set(value) - set(stable_ids))
    if strangers:
        raise ToolFailure("invalid_arguments", f"etags names tracks not in stable_ids: {strangers}")
    return value


# ----- tools --------------------------------------------------------------
async def _track_update(ctx: ToolContext, args: dict[str, Any]) -> Any:
    stable_id = str(text_arg(args, "stable_id", required=True))
    fields = _edit_fields(args)
    etag = text_arg(args, "etag") or await _current_etag(ctx, stable_id)
    answer = await engine_call(ctx, "PATCH", _track_path(stable_id), fields, if_match=etag)
    row = answer.body if isinstance(answer.body, dict) else {}
    return {"etag": answer.etag, "track": {name: row[name] for name in ROW_FIELDS if name in row}}


async def _tracks_bulk_edit(ctx: ToolContext, args: dict[str, Any]) -> Any:
    stable_ids = _text_list(args, "stable_ids", high=MAX_STABLE_IDS)
    if stable_ids is None:
        raise ToolFailure("invalid_arguments", "stable_ids is required")
    fields = _edit_fields(args)
    etags = _given_etags(args, stable_ids)
    for stable_id in stable_ids:
        if stable_id not in etags:
            etags[stable_id] = await _current_etag(ctx, stable_id)
    body = {"stable_ids": stable_ids, "expected_etags": etags, **fields}
    return (await engine_call(ctx, "PATCH", BULK_EDIT_PATH, body)).body


_STRING = {"type": "string"}
_TAGS = {"type": "array", "items": _STRING, "minItems": 1, "maxItems": MAX_STABLE_IDS, "uniqueItems": True}
_FIELDS: dict[str, Any] = {
    "rating": {"type": "integer", "minimum": 0, "maximum": MAX_RATING, "description": "Stars, 0 to 5."},
    "genre": {**_STRING, "description": "The new genre, e.g. Afro House."},
    "comments": {**_STRING, "description": "The new comment (replaces the old one)."},
    "notes": {**_STRING, "description": "The new private notes (replace the old ones)."},
    "tags_add": {**_TAGS, "description": "Tags to add, exactly as spelled."},
    "tags_remove": {**_TAGS, "description": "Tags to remove, exactly as spelled."},
}

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="track_update",
        description=(
            "Change one track's rating, genre, comments, notes or tags (give at least one), as the "
            "track editor does. Reads the track's etag itself; pass etag from track_get to refuse "
            "the write if the track changed since. A changed track fails engine_error naming 409 "
            "and nothing is written. Answers {etag, track}. "
            "CLI: opendj api PATCH /api/v1/tracks/{stable_id} -H If-Match:<etag>; "
            "MCP: track_set_comment (comments only)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "stable_id": {**_STRING, "description": "The track's stable_id (from library_search)."},
                "etag": {**_STRING, "description": "The etag track_get returned; omit to use the current one."},
                **_FIELDS,
            },
            "required": ["stable_id"],
        },
        gate="confirm",
        run=_track_update,
    ),
    ToolSpec(
        name="tracks_bulk_edit",
        description=(
            "Set the same rating, genre, comments, notes or tags (give at least one) on up to "
            f"{MAX_STABLE_IDS} tracks at once, as the bulk editor does, e.g. rate five tracks 4 stars. "
            "Reads each track's etag itself unless etags maps it. If any track changed, the engine's "
            "409 fails engine_error and no track is written. Answers {applied_count, results}. "
            "CLI: opendj api PATCH /api/v1/bulk-edit; MCP: none."
        ),
        parameters={
            "type": "object",
            "properties": {
                "stable_ids": {
                    "type": "array",
                    "items": _STRING,
                    "minItems": 1,
                    "maxItems": MAX_STABLE_IDS,
                    "uniqueItems": True,
                    "description": "The tracks to edit (stable_ids from library_search).",
                },
                "etags": {
                    "type": "object",
                    "additionalProperties": _STRING,
                    "description": "Optional stable_id to etag (from track_get); missing ones are read.",
                },
                **_FIELDS,
            },
            "required": ["stable_ids"],
        },
        gate="confirm",
        run=_tracks_bulk_edit,
    ),
)
