"""CloudSync from OJ: ``cloudsync_status``, ``playlist_pin_list``, ``playlist_pin_set``.

So "keep this playlist on my laptop" works: OJ reads the sync state, lists a
machine's playlist pins and pins one playlist to one machine with a mode.
Machine ids come from ``GET /api/v1/cloudsync/machines`` (``library_read``).

There is deliberately no sync-run tool: ``POST /api/v1/cloudsync/sync`` moves
data over the network, and the scheduler and the UI own it. Every route this
module calls is in :data:`ROUTES`. A pin is a policy write
(``routes/cloudsync.py``): the engine's policy gate answers 404 for an unknown
machine or playlist and 409 for a violation, both ``engine_error``.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from apps.engine_core.assistant.agent_contract import ToolContext, ToolFailure, ToolSpec
from apps.engine_core.assistant.core_tools import engine_json
from apps.engine_core.assistant.oj_tools._args import text_arg

# ----- configuration ------------------------------------------------------
STATUS_PATH: str = "/api/v1/cloudsync/status"
PINS_PATH: str = "/api/v1/cloudsync/playlist-pins"
#: Every engine route a tool here calls; the sync run is never one of them.
ROUTES: tuple[str, ...] = (STATUS_PATH, PINS_PATH)

#: The pin route's own ``SyncMode`` literal, in its order (pinned by a test).
PIN_MODES: tuple[str, ...] = ("pinned", "cached", "stream", "excluded")

#: The status fields the model needs; the five-result journal and the digest sample stay out.
STATUS_FIELDS: tuple[str, ...] = (
    "enabled",
    "configured",
    "running",
    "reason",
    "signed_in_as",
    "endpoint",
    "heartbeat_at",
    "last_push_at",
    "last_result",
    "hash_pending",
    "quarantined",
    "excluded_total",
    "update_required",
    "credential_notice",
)
PIN_FIELDS: tuple[str, ...] = ("machine_id", "playlist_id", "playlist_name", "mode")


# ----- helpers ------------------------------------------------------------
def _project(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {name: row[name] for name in fields if name in row}


def _mode_arg(args: dict[str, Any]) -> str:
    mode = args.get("mode")
    if mode not in PIN_MODES:
        raise ToolFailure("invalid_arguments", f"mode must be one of {', '.join(PIN_MODES)}, got {mode!r}")
    return str(mode)


# ----- tools --------------------------------------------------------------
async def _cloudsync_status(ctx: ToolContext, _args: dict[str, Any]) -> Any:
    return _project(await engine_json(ctx, "GET", STATUS_PATH), STATUS_FIELDS)


async def _playlist_pin_list(ctx: ToolContext, args: dict[str, Any]) -> Any:
    machine_id = text_arg(args, "machine_id")
    query = f"?{urlencode({'machine_id': machine_id})}" if machine_id is not None else ""
    rows = await engine_json(ctx, "GET", f"{PINS_PATH}{query}")
    return {"pins": [_project(row, PIN_FIELDS) for row in rows]}


async def _playlist_pin_set(ctx: ToolContext, args: dict[str, Any]) -> Any:
    body = {
        "machine_id": text_arg(args, "machine_id", required=True),
        "playlist_id": text_arg(args, "playlist_id", required=True),
        "mode": _mode_arg(args),
    }
    return _project(await engine_json(ctx, "PUT", PINS_PATH, body), PIN_FIELDS)


_MACHINE_ID = {"type": "string", "description": "A machine_id from GET /api/v1/cloudsync/machines."}

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="cloudsync_status",
        description=(
            "Is CloudSync on and working on this machine: enabled, configured, running, the reason "
            "when not, the hub endpoint, the last push and its result, rows held back, and any "
            "update or re-enroll notice. Reads only; never starts a sync. "
            "CLI: uv run python -m apps.sync_hub status --data-dir DIR; "
            "MCP: app_state with /api/v1/cloudsync/status."
        ),
        parameters={"type": "object", "properties": {}},
        gate="read",
        run=_cloudsync_status,
    ),
    ToolSpec(
        name="playlist_pin_list",
        description=(
            "Which playlists are pinned to which machine, and how (mode pinned, cached, stream or "
            "excluded). Give machine_id for one machine; omit it for every machine. Answers {pins}; "
            "each pin has machine_id, playlist_id, playlist_name, mode. "
            "CLI: uv run python -m apps.sync_hub policy show --machine ID --data-dir DIR; "
            "MCP: app_state with /api/v1/cloudsync/playlist-pins?machine_id=ID."
        ),
        parameters={"type": "object", "properties": {"machine_id": _MACHINE_ID}},
        gate="read",
        run=_playlist_pin_list,
    ),
    ToolSpec(
        name="playlist_pin_set",
        description=(
            "Pin one playlist to one machine: pinned keeps its tracks on that machine, cached keeps "
            "them while space allows, stream plays them from the cloud, excluded keeps them off. "
            "Replaces that machine's earlier pin for the playlist. An unknown machine or playlist "
            "fails engine_error naming 404; a policy violation fails engine_error naming 409 and "
            "writes nothing. Answers the stored pin. "
            "CLI: uv run python -m apps.sync_hub policy pin --machine ID --playlist ID --mode M --live; "
            "MCP: library PUT /api/v1/cloudsync/playlist-pins."
        ),
        parameters={
            "type": "object",
            "properties": {
                "machine_id": _MACHINE_ID,
                "playlist_id": {"type": "string", "description": "The playlist's playlist_id."},
                "mode": {"type": "string", "enum": list(PIN_MODES)},
            },
            "required": ["machine_id", "playlist_id", "mode"],
        },
        gate="confirm",
        run=_playlist_pin_set,
    ),
)
