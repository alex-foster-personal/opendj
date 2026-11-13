"""Record a set from OJ: ``recorder_status``, ``recorder_start`` and ``recorder_stop``.

The same ``/api/sets/recorder`` routes the REC button and ``opendj rec`` use,
so "record this set" from OJ starts exactly what the button would. The engine
owns every check on what to record: ``source`` is passed through, and a
request the engine refuses as malformed (400 or 422) comes back as
``invalid_arguments`` with its reason. Every other refusal (409 already
recording, 403 recording turned off, 503 no input or no open page) stays
``engine_error`` naming its status. ``recorder_stop`` with no ``session_id``
stops the session ``recorder_status`` reports as active, and refuses before
sending any stop when nothing is recording.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from apps.engine_core.assistant.agent_contract import ToolContext, ToolFailure, ToolSpec
from apps.engine_core.assistant.core_tools import engine_call
from apps.engine_core.assistant.oj_tools._args import text_arg

# ----- configuration ------------------------------------------------------
RECORDER_PATH: str = "/api/sets/recorder"
#: What REC can record; the engine's ``RecordSource``, listed for the model only.
SOURCES: tuple[str, ...] = ("master", "loopback", "external", "none")
#: Engine answers that mean the request itself was malformed, not the state.
REFUSED_REQUEST_STATUSES: tuple[int, ...] = (400, 422)


# ----- helpers ------------------------------------------------------------
async def _recorder_write(ctx: ToolContext, path: str, body: dict[str, Any] | None = None) -> Any:
    """One recorder POST; a 400 or 422 becomes ``invalid_arguments`` with the engine's reason."""
    try:
        return (await engine_call(ctx, "POST", path, body)).body
    except ToolFailure as exc:
        if exc.code == "engine_error" and any(f" -> {status}: " in exc.message for status in REFUSED_REQUEST_STATUSES):
            raise ToolFailure("invalid_arguments", f"the engine refused this request: {exc.message}") from exc
        raise


async def _active_session_id(ctx: ToolContext) -> str:
    """The session recorder_status reports as active; fails before any stop when there is none."""
    status = (await engine_call(ctx, "GET", RECORDER_PATH)).body
    session_id = status.get("session_id") if isinstance(status, dict) else None
    if not isinstance(status, dict) or not status.get("active") or not isinstance(session_id, str):
        raise ToolFailure("invalid_arguments", "nothing_recording: no recording is active, so there is nothing to stop")
    return session_id


# ----- tools --------------------------------------------------------------
async def _recorder_status(ctx: ToolContext, _args: dict[str, Any]) -> Any:
    return (await engine_call(ctx, "GET", RECORDER_PATH)).body


async def _recorder_start(ctx: ToolContext, args: dict[str, Any]) -> Any:
    source = text_arg(args, "source", required=True)
    device_name = text_arg(args, "device_name")
    body = {"source": source, **({"device_name": device_name} if device_name is not None else {})}
    return await _recorder_write(ctx, f"{RECORDER_PATH}/start", body)


async def _recorder_stop(ctx: ToolContext, args: dict[str, Any]) -> Any:
    session_id = text_arg(args, "session_id") or await _active_session_id(ctx)
    return await _recorder_write(ctx, f"{RECORDER_PATH}/{quote(session_id, safe='')}/stop")


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="recorder_status",
        description=(
            "Whether a set is being recorded: {active, session_id, capture, capture_error, "
            "capture_source, elapsed_s, recordings_dir, ...} exactly as the engine reports it. "
            "CLI: opendj rec status; MCP: recorder_status."
        ),
        parameters={"type": "object", "properties": {}},
        gate="read",
        run=_recorder_status,
    ),
    ToolSpec(
        name="recorder_start",
        description=(
            "Start recording the set. source master records Open DJ's own master mix (needs the "
            "performance page open); loopback and external record one input named by device_name; "
            "none records the tracklist only. A request the engine refuses fails invalid_arguments "
            "with its reason; already recording fails engine_error naming 409. "
            "CLI: opendj rec start --source master; MCP: recorder_start."
        ),
        parameters={
            "type": "object",
            "properties": {
                "source": {"type": "string", "enum": list(SOURCES)},
                "device_name": {
                    "type": "string",
                    "description": "The input to record, for loopback or external only (e.g. BlackHole 2ch).",
                },
            },
            "required": ["source"],
        },
        gate="confirm",
        run=_recorder_start,
    ),
    ToolSpec(
        name="recorder_stop",
        description=(
            "Stop and finalize the recording. With no session_id it stops the session "
            "recorder_status reports as active; when nothing is recording it fails "
            "invalid_arguments starting nothing_recording and sends no stop. "
            "CLI: opendj rec stop; MCP: recorder_stop."
        ),
        parameters={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "A session id from recorder_status."},
            },
        },
        gate="confirm",
        run=_recorder_stop,
    ),
)
