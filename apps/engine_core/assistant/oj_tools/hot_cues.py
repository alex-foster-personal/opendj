"""A track's hot cues: ``hot_cues_list``, ``hot_cue_set`` and ``hot_cue_clear``.

These wrap the track-scoped cue store routes (``apps/webui/server/routes/rb_hot_cues.py``),
which keep cues in Open DJ's own ``state.db`` and never write rekordbox. Every
mutation there needs the slot's revision as ``If-Match``; the list answer carries
one revision per slot (it sends no ``ETag`` header), so each write tool reads the
list itself and passes that slot's revision on, and the model never handles one.
A slot outside :data:`SLOTS` (the route's ``Literal['A'..'H']``) fails
``invalid_arguments`` before any request.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from apps.engine_core.assistant.agent_contract import ToolContext, ToolFailure, ToolSpec
from apps.engine_core.assistant.core_tools import engine_call, engine_json
from apps.engine_core.assistant.oj_tools._args import int_arg, text_arg

# ----- configuration ------------------------------------------------------
TRACKS_PATH: str = "/api/v1/tracks"
#: The engine's hot cue slots, in order (the route's ``HotCueSlot`` literal).
SLOTS: tuple[str, ...] = ("A", "B", "C", "D", "E", "F", "G", "H")
#: What one set cue carries for the model; revisions stay inside the tools.
CUE_FIELDS: tuple[str, ...] = ("in_ms", "out_ms", "is_loop", "comment", "color_table_index")
#: Upper bound for a position here: a day. The engine bounds it by the track's length.
MAX_POSITION_MS: int = 86_400_000
#: The engine only asks for >= 0; rekordbox's hot cue colour table is far smaller than this.
MAX_COLOR_INDEX: int = 255


# ----- helpers ------------------------------------------------------------
def _stable_id_arg(args: dict[str, Any]) -> str:
    return str(text_arg(args, "stable_id", required=True))


def _slot_arg(args: dict[str, Any]) -> str:
    """The slot letter, upper-cased; anything outside SLOTS fails before a request."""
    slot = args.get("slot")
    if not isinstance(slot, str) or slot.strip().upper() not in SLOTS:
        raise ToolFailure("invalid_arguments", f"slot must be one of {', '.join(SLOTS)}, got {slot!r}")
    return slot.strip().upper()


def _cues_path(stable_id: str) -> str:
    return f"{TRACKS_PATH}/{quote(stable_id, safe='')}/hot-cues"


def _compact(cue: dict[str, Any] | None) -> dict[str, Any] | None:
    return None if cue is None else {name: cue.get(name) for name in CUE_FIELDS}


async def _write_slot(ctx: ToolContext, method: str, stable_id: str, slot: str, body: Any = None) -> Any:
    """Read the slot's revision from the list, then send the write with it as If-Match."""
    rows = await engine_json(ctx, "GET", _cues_path(stable_id))
    revision = next(str(row["revision"]) for row in rows if row["slot"] == slot)
    # A mutation answer is one cue (a few hundred bytes), so engine_call's cap never cuts it.
    answer = await engine_call(ctx, method, f"{_cues_path(stable_id)}/{slot}", body, if_match=revision)
    return answer.body


# ----- tools --------------------------------------------------------------
async def _hot_cues_list(ctx: ToolContext, args: dict[str, Any]) -> Any:
    stable_id = _stable_id_arg(args)
    rows = await engine_json(ctx, "GET", _cues_path(stable_id))
    return {"stable_id": stable_id, "slots": [{"slot": row["slot"], "cue": _compact(row["cue"])} for row in rows]}


async def _hot_cue_set(ctx: ToolContext, args: dict[str, Any]) -> Any:
    stable_id = _stable_id_arg(args)
    slot = _slot_arg(args)
    if args.get("in_ms") is None:
        raise ToolFailure("invalid_arguments", "in_ms is required")
    body = {
        "in_ms": int_arg(args, "in_ms", low=0, high=MAX_POSITION_MS),
        "out_ms": int_arg(args, "out_ms", low=0, high=MAX_POSITION_MS),
        "comment": text_arg(args, "comment"),
        "color_table_index": int_arg(args, "color_table_index", low=0, high=MAX_COLOR_INDEX),
    }
    answer = await _write_slot(ctx, "PUT", stable_id, slot, body)
    return {"stable_id": stable_id, "slot": slot, "cue": _compact(answer["cue"])}


async def _hot_cue_clear(ctx: ToolContext, args: dict[str, Any]) -> Any:
    stable_id = _stable_id_arg(args)
    slot = _slot_arg(args)
    await _write_slot(ctx, "DELETE", stable_id, slot)
    return {"stable_id": stable_id, "slot": slot, "cue": None}


_STABLE_ID = {"type": "string", "description": "The track's stable_id (from library_search)."}
_SLOT = {"type": "string", "enum": list(SLOTS), "description": "Hot cue slot letter."}

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="hot_cues_list",
        description=(
            "A track's eight hot cue slots A-H, each {slot, cue}; cue is null for an empty slot, "
            "else {in_ms, out_ms, is_loop, comment, color_table_index} (out_ms set means a loop "
            "hot cue). An unknown id fails engine_error naming 404. "
            "CLI: opendj api GET /api/v1/tracks/{stable_id}/hot-cues; MCP: app_state with that path."
        ),
        parameters={"type": "object", "properties": {"stable_id": _STABLE_ID}, "required": ["stable_id"]},
        gate="read",
        run=_hot_cues_list,
    ),
    ToolSpec(
        name="hot_cue_set",
        description=(
            "Save one hot cue on a track (not a deck): slot A-H at in_ms, replacing what the "
            "slot held. out_ms (after in_ms) makes it a loop hot cue. Open DJ's own cue store, "
            "never rekordbox. Answers {stable_id, slot, cue}. "
            "CLI: opendj api PUT /api/v1/tracks/{stable_id}/hot-cues/{slot} with If-Match; "
            "MCP: library with that path. For a loaded deck use command hot_cue_save."
        ),
        parameters={
            "type": "object",
            "properties": {
                "stable_id": _STABLE_ID,
                "slot": _SLOT,
                "in_ms": {"type": "integer", "minimum": 0, "description": "Cue position in ms."},
                "out_ms": {"type": "integer", "minimum": 0, "description": "Loop end in ms; omit for a point cue."},
                "comment": {"type": "string", "description": "The cue's label."},
                "color_table_index": {"type": "integer", "minimum": 0, "maximum": MAX_COLOR_INDEX},
            },
            "required": ["stable_id", "slot", "in_ms"],
        },
        gate="confirm",
        run=_hot_cue_set,
    ),
    ToolSpec(
        name="hot_cue_clear",
        description=(
            "Empty one hot cue slot A-H on a track (not a deck). Answers {stable_id, slot, cue: null}. "
            "CLI: opendj api DELETE /api/v1/tracks/{stable_id}/hot-cues/{slot} with If-Match; "
            "MCP: library with that path. For a loaded deck use command hot_cue_clear."
        ),
        parameters={
            "type": "object",
            "properties": {"stable_id": _STABLE_ID, "slot": _SLOT},
            "required": ["stable_id", "slot"],
        },
        gate="confirm",
        run=_hot_cue_clear,
    ),
)
