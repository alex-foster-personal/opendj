"""Build sets from a playlist: ``set_list``, ``set_show``, ``set_create``,
``set_update_parts``, ``set_target`` and ``set_skip``.

Two things share the word "set" here, both under ``/api/v1/playlists/{id}``:

- Named sets (``.../sets``, SET-05): a saved snapshot of the playlist's order,
  or a route of component ids (``parts``, SET-6586), with a play count.
- The playlist played as a set (``.../set-summary``, ``set-target``,
  ``set-skips``, LIBUX-8679): one target length and a skip mark per track,
  summarised as played length, over/under and key clashes.

``set_show`` answers both for one playlist. Set rows come back as
:func:`_compact_set`: entries as ordered stable_ids, no timestamps per entry.
None of these routes takes ``If-Match``.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from apps.engine_core.assistant.agent_contract import ToolContext, ToolFailure, ToolSpec
from apps.engine_core.assistant.core_tools import engine_call, engine_json
from apps.engine_core.assistant.oj_tools._args import int_arg, number_arg, text_arg

# ----- configuration ------------------------------------------------------
PLAYLISTS_PATH: str = "/api/v1/playlists"
#: A sqlite rowid bound, so a set id the model invents fails here, not in the engine.
MAX_ID: int = 2**63 - 1
#: Twelve hours: no set is longer, so a bigger target is a unit slip (minutes for seconds).
MAX_TARGET_S: float = 12 * 3600.0
MAX_PARTS: int = 64


# ----- helpers ------------------------------------------------------------
def _playlist_path(args: dict[str, Any]) -> str:
    playlist_id = text_arg(args, "playlist_id", required=True)
    return f"{PLAYLISTS_PATH}/{quote(str(playlist_id), safe='')}"


def _set_id(args: dict[str, Any], *, required: bool) -> int | None:
    set_id = int_arg(args, "set_id", low=1, high=MAX_ID)
    if set_id is None and required:
        raise ToolFailure("invalid_arguments", "set_id is required")
    return set_id


def _parts(args: dict[str, Any], *, required: bool) -> list[int] | None:
    """A route of component ids: whole numbers, at least one, none a boolean."""
    value = args.get("parts")
    if value is None and not required:
        return None
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= MAX_PARTS
        or not all(isinstance(part, int) and not isinstance(part, bool) and part >= 1 for part in value)
    ):
        raise ToolFailure("invalid_arguments", f"parts must be 1 to {MAX_PARTS} component ids, got {value!r}")
    return value


def _flag(args: dict[str, Any], name: str) -> bool:
    value = args.get(name)
    if not isinstance(value, bool):
        raise ToolFailure("invalid_arguments", f"{name} must be true or false, got {value!r}")
    return value


def _compact_set(row: dict[str, Any], *, with_entries: bool) -> dict[str, Any]:
    """One named set for the model: entries as ordered ids (or just their count), parts, play count."""
    entries = [entry["stable_id"] for entry in sorted(row["entries"], key=lambda entry: entry["position"])]
    out: dict[str, Any] = {
        "set_id": row["id"],
        "name": row["name"],
        "play_count": row["play_count"],
        "parts": row["parts"],
        "entry_count": len(entries),
        "updated_at": row["updated_at"],
    }
    if with_entries:
        out["entries"] = entries
    if row.get("runs") is not None:
        out["runs"] = [{"kind": run["kind"], "created_at": run["created_at"]} for run in row["runs"]]
    return out


# ----- tools --------------------------------------------------------------
async def _set_list(ctx: ToolContext, args: dict[str, Any]) -> Any:
    page = await engine_json(ctx, "GET", f"{_playlist_path(args)}/sets")
    return {"sets": [_compact_set(row, with_entries=False) for row in page["sets"]]}


async def _set_show(ctx: ToolContext, args: dict[str, Any]) -> Any:
    base = _playlist_path(args)
    set_id = _set_id(args, required=False)
    summary = await engine_json(ctx, "GET", f"{base}/set-summary")
    named = None if set_id is None else await engine_json(ctx, "GET", f"{base}/sets/{set_id}")
    return {"set": None if named is None else _compact_set(named, with_entries=True), "summary": summary}


async def _set_create(ctx: ToolContext, args: dict[str, Any]) -> Any:
    base = _playlist_path(args)
    body = {
        "name": text_arg(args, "name", required=True),
        "from_play_order": text_arg(args, "from_play_order"),
        "parts": _parts(args, required=False),
    }
    if body["parts"] is not None and body["from_play_order"] is not None:
        raise ToolFailure("invalid_arguments", "give parts or from_play_order, not both")
    created = await engine_json(ctx, "POST", f"{base}/sets", body)
    return {"set": _compact_set(created, with_entries=True)}


async def _set_update_parts(ctx: ToolContext, args: dict[str, Any]) -> Any:
    base = _playlist_path(args)
    set_id = _set_id(args, required=True)
    parts = _parts(args, required=True)
    updated = await engine_json(ctx, "PUT", f"{base}/sets/{set_id}/parts", {"parts": parts})
    return {"set": _compact_set(updated, with_entries=True)}


async def _set_target(ctx: ToolContext, args: dict[str, Any]) -> Any:
    base = _playlist_path(args)
    if "target_s" not in args:
        raise ToolFailure("invalid_arguments", "target_s is required: seconds (3600 is an hour), or null to clear")
    target_s = number_arg(args, "target_s", low=1.0, high=MAX_TARGET_S)
    return (await engine_call(ctx, "PUT", f"{base}/set-target", {"target_s": target_s})).body


async def _set_skip(ctx: ToolContext, args: dict[str, Any]) -> Any:
    base = _playlist_path(args)
    stable_id = text_arg(args, "stable_id", required=True)
    skipped = _flag(args, "skipped")
    path = f"{base}/set-skips/{quote(str(stable_id), safe='')}"
    return (await engine_call(ctx, "PUT", path, {"skipped": skipped})).body


_PLAYLIST_ID = {"type": "string", "description": "The playlist's playlist_id."}
_SET_ID = {"type": "integer", "minimum": 1, "description": "A named set's set_id (from set_list)."}
_PARTS = {
    "type": "array",
    "items": {"type": "integer", "minimum": 1},
    "minItems": 1,
    "maxItems": MAX_PARTS,
    "description": "Component ids in slot order (GET /api/v1/playlists/{id}/components).",
}
_SUMMARY = (
    "The summary is {playlist_id, played_s, target_s, over_s, skipped, skipped_ids, clashes, readout}; "
    "played_s leaves skipped tracks out, over_s is negative while under target."
)

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="set_list",
        description=(
            "The named sets saved on one playlist: each {set_id, name, play_count, parts, entry_count, "
            "updated_at}. An unknown playlist fails engine_error naming 404. "
            "CLI: opendj api GET /api/v1/playlists/{playlist_id}/sets; MCP: app_state with that path."
        ),
        parameters={
            "type": "object",
            "properties": {"playlist_id": _PLAYLIST_ID},
            "required": ["playlist_id"],
        },
        gate="read",
        run=_set_list,
    ),
    ToolSpec(
        name="set_show",
        description=(
            "One playlist as a set: its summary (target length, played length, skips, key clashes) and, "
            "with set_id, that named set's ordered entries, parts and runs. " + _SUMMARY + " "
            "CLI: opendj set summary <playlist_id>; MCP: set_summary."
        ),
        parameters={
            "type": "object",
            "properties": {"playlist_id": _PLAYLIST_ID, "set_id": _SET_ID},
            "required": ["playlist_id"],
        },
        gate="read",
        run=_set_show,
    ),
    ToolSpec(
        name="set_create",
        description=(
            "Save a named set on a playlist: a snapshot of its current order (or of the play order "
            "from_play_order), or a route of component ids (parts). Answers {set}. "
            "CLI: opendj api POST /api/v1/playlists/{playlist_id}/sets; MCP: none."
        ),
        parameters={
            "type": "object",
            "properties": {
                "playlist_id": _PLAYLIST_ID,
                "name": {"type": "string", "description": "The set's name, unique on the playlist."},
                "from_play_order": {"type": "string", "description": "A saved play order's name to snapshot."},
                "parts": _PARTS,
            },
            "required": ["playlist_id", "name"],
        },
        gate="confirm",
        run=_set_create,
    ),
    ToolSpec(
        name="set_update_parts",
        description=(
            "Replace a named set's route with new component ids; past runs keep what they played. "
            "Answers {set}. CLI: opendj api PUT /api/v1/playlists/{playlist_id}/sets/{set_id}/parts; MCP: none."
        ),
        parameters={
            "type": "object",
            "properties": {"playlist_id": _PLAYLIST_ID, "set_id": _SET_ID, "parts": _PARTS},
            "required": ["playlist_id", "set_id", "parts"],
        },
        gate="confirm",
        run=_set_update_parts,
    ),
    ToolSpec(
        name="set_target",
        description=(
            "Set the playlist's target set length in seconds (3600 is an hour), or null to clear it. "
            "Answers the new summary. " + _SUMMARY + " "
            "CLI: opendj set target <playlist_id> <minutes>|clear; MCP: set_target."
        ),
        parameters={
            "type": "object",
            "properties": {
                "playlist_id": _PLAYLIST_ID,
                "target_s": {"type": ["number", "null"], "minimum": 1, "maximum": MAX_TARGET_S},
            },
            "required": ["playlist_id", "target_s"],
        },
        gate="confirm",
        run=_set_target,
    ),
    ToolSpec(
        name="set_skip",
        description=(
            "Mark one playlist track skip-in-set (kept as a backup, left out of the played length), "
            "or skipped=false to bring it back. Answers the new summary. "
            "CLI: opendj set skip <playlist_id> <stable_id> [off]; MCP: set_skip."
        ),
        parameters={
            "type": "object",
            "properties": {
                "playlist_id": _PLAYLIST_ID,
                "stable_id": {"type": "string", "description": "A track in the playlist."},
                "skipped": {"type": "boolean"},
            },
            "required": ["playlist_id", "stable_id", "skipped"],
        },
        gate="confirm",
        run=_set_skip,
    ),
)
