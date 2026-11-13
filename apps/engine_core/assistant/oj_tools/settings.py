"""Read and set the user's preferences, and undo a playlist edit.

``settings_get`` reads the persisted UI preferences (``GET /api/v1/ui-prefs``)
and the engine's read-only runtime settings (``GET /api/v1/settings``).
``ui_pref_set`` changes ONE ui-pref: it reads the prefs first and refuses a
key the engine does not answer, because ``PUT /api/v1/ui-prefs`` ignores
unknown keys silently; the PUT is a merge patch carrying only that key, so no
other pref moves. ``playlist_undo`` steps back the last playlist edit
(``POST /api/v1/playlist-history/undo``), so "undo that" works after OJ
renames, reorders or edits a playlist.

Left out on purpose: ``POST /api/v1/settings/ai-search`` asks a hosted model
for every call (a tool should not spend a model call to find a setting the
prefs answer already names), and ``POST /api/v1/undo`` needs an
``X-Undo-Session`` header that ``engine_call`` cannot send yet (#13836).
"""

from __future__ import annotations

from typing import Any

from apps.engine_core.assistant.agent_contract import ToolContext, ToolFailure, ToolSpec
from apps.engine_core.assistant.core_tools import engine_call, engine_json
from apps.engine_core.assistant.oj_tools._args import text_arg

# ----- configuration ------------------------------------------------------
UI_PREFS_PATH: str = "/api/v1/ui-prefs"
SETTINGS_PATH: str = "/api/v1/settings"
PLAYLIST_UNDO_PATH: str = "/api/v1/playlist-history/undo"

#: What one playlist undo answer carries for the model: the playlist's items list is dropped.
UNDO_FIELDS: tuple[str, ...] = ("command_id", "op", "action", "playlist_id", "etag", "can_undo", "can_redo")


# ----- helpers ------------------------------------------------------------
def _unknown_keys(asked: list[str], prefs: dict[str, Any]) -> ToolFailure:
    return ToolFailure(
        "invalid_arguments",
        f"unknown ui-pref key(s) {asked}; known keys: {', '.join(sorted(prefs))}",
    )


def _keys_arg(args: dict[str, Any]) -> list[str] | None:
    """The optional ``keys`` list: non-empty strings, or None when absent."""
    keys = args.get("keys")
    if keys is None:
        return None
    if not isinstance(keys, list) or not keys or not all(isinstance(key, str) and key for key in keys):
        raise ToolFailure("invalid_arguments", f"keys must be a non-empty list of ui-pref names, got {keys!r}")
    return keys


# ----- tools --------------------------------------------------------------
async def _settings_get(ctx: ToolContext, args: dict[str, Any]) -> Any:
    keys = _keys_arg(args)
    prefs = await engine_json(ctx, "GET", UI_PREFS_PATH)
    if keys is None:
        engine = await engine_call(ctx, "GET", SETTINGS_PATH)
        return {"ui_prefs": prefs, "engine_settings": engine.body}
    unknown = [key for key in keys if key not in prefs]
    if unknown:
        raise _unknown_keys(unknown, prefs)
    return {"ui_prefs": {key: prefs[key] for key in keys}}


async def _ui_pref_set(ctx: ToolContext, args: dict[str, Any]) -> Any:
    key = str(text_arg(args, "key", required=True))
    if "value" not in args:
        raise ToolFailure("invalid_arguments", "value is required (the new value for the key)")
    before = await engine_json(ctx, "GET", UI_PREFS_PATH)
    if key not in before:
        raise _unknown_keys([key], before)
    after = await engine_json(ctx, "PUT", UI_PREFS_PATH, {key: args["value"]})
    return {"key": key, "before": before[key], "after": after[key]}


async def _playlist_undo(ctx: ToolContext, _args: dict[str, Any]) -> Any:
    answer = await engine_json(ctx, "POST", PLAYLIST_UNDO_PATH)
    current = answer.get("current") or {}
    return {
        **{name: answer[name] for name in UNDO_FIELDS if name in answer},
        "name": current.get("name"),
        "track_count": current.get("track_count"),
    }


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="settings_get",
        description=(
            "Read the user's UI preferences (theme, library density, confirm prompts, auto sync, "
            "waveform and deck options, and more) plus the engine's read-only runtime settings. "
            "Pass keys to read only those ui-prefs; an unknown key fails invalid_arguments naming "
            "the known ones. CLI: opendj api GET /api/v1/ui-prefs and GET /api/v1/settings; "
            "MCP: app_state with the same paths."
        ),
        parameters={
            "type": "object",
            "properties": {
                "keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": 'ui-pref names to read, e.g. ["theme"]; omit for everything.',
                },
            },
        },
        gate="read",
        run=_settings_get,
    ),
    ToolSpec(
        name="ui_pref_set",
        description=(
            "Set ONE UI preference to a new value; every other preference stays as it is. "
            "Answers {key, before, after}. An unknown key fails invalid_arguments and nothing is "
            "written; a value the engine refuses fails engine_error naming 422. Read settings_get "
            "first for the key's current value and shape. CLI: opendj api PUT /api/v1/ui-prefs "
            "--json '{\"<key>\": <value>}'; MCP: library with method PUT and that path."
        ),
        parameters={
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "A ui-pref name from settings_get, e.g. theme."},
                "value": {"description": "The new value, in the shape settings_get shows for that key."},
            },
            "required": ["key", "value"],
        },
        gate="confirm",
        run=_ui_pref_set,
    ),
    ToolSpec(
        name="playlist_undo",
        description=(
            "Undo the last playlist edit (a rename, reorder, add or remove), like the playlist "
            "history's undo. Answers the undone op, the playlist's id, name and track_count now, "
            "and whether more can be undone or redone. Nothing to undo fails engine_error naming "
            "409 nothing_to_undo. CLI: opendj api POST /api/v1/playlist-history/undo; "
            "MCP: library with method POST and that path."
        ),
        parameters={"type": "object", "properties": {}},
        gate="confirm",
        run=_playlist_undo,
    ),
)
