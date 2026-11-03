"""The assistant's own-engine tools: status, app state, library, commands.

Every tool reaches the engine through its public ``/api/v1`` routes at
``ToolContext.base_url`` and nothing else, so the assistant can do exactly
what an HTTP client (curl, ``opendj``, the MCP server) can do and no more.
One helper, :func:`_engine`, makes every call and is the only place an HTTP
failure becomes a :class:`ToolFailure` code:

- ``engine_unreachable``: the request never got an answer.
- ``no_performance_page``: the documented ``409 {"client_open": false}``.
- ``engine_error``: any other non-2xx, with the engine's own words.

The library tools apply the MCP server's rails (``validate_api_path``,
``guard_library_request``), which judge the canonical path the engine routes,
while the request carries the path as given. The ``command`` tool does NOT apply
``guard_order``: its only effect is to prepend a master mute to an audible
play, which is right for an unattended MCP agent and wrong here, where the
user approves every command and a hidden mute would make "play deck 2"
silent. Order shape is validated by ``POST /api/v1/commands`` itself (422).
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from typing import Any

import httpx

from apps.engine_core.assistant.agent_contract import ToolContext, ToolFailure, ToolSpec
from apps.opendj_cli.catalog import VERBS
from apps.opendj_cli.mcp_safety import SafetyRefusal, guard_library_request, validate_api_path
from apps.opendj_cli.orders import ramp_deadline_s, single
from apps.opendj_cli.verbs import InvocationError, parse_invocation
from apps.shared.telemetry.live import mirror_decks_live

# ----- configuration ------------------------------------------------------
#: The most of one engine answer the model sees. A track list can be
#: megabytes; past this the model gets the head and the true size, and is
#: told to page or project (``?limit=``, ``?fields=``).
MAX_TOOL_RESULT_CHARS: int = 12_000

#: A command waits for the page to claim it (15 s) and then for its result;
#: a ramp longer than this gets its own deadline (:func:`_command_read_s`).
_READ_S: float = 90.0

MIRROR_PATH: str = "/api/v1/state/ui-mirror"
COMMANDS_PATH: str = "/api/v1/commands"
_WRITE_METHODS: tuple[str, ...] = ("POST", "PUT", "PATCH", "DELETE")
#: The assistant must not drive itself: a library call into its own routes
#: would spend a second model turn per tool call.
_SELF_PREFIX: str = "/api/v1/assistant"


# ----- helpers ------------------------------------------------------------
def _parsed(response: httpx.Response) -> Any:
    """The whole answer as JSON (or text): for this module to judge, never for the model."""
    if not response.text:
        return None
    try:
        return response.json()
    except ValueError:
        return response.text


def _bounded(response: httpx.Response) -> Any:
    """The answer for the model: as parsed, or cut to MAX_TOOL_RESULT_CHARS with its true size."""
    text = response.text
    if len(text) > MAX_TOOL_RESULT_CHARS:
        return {"truncated": True, "total_chars": len(text), "head": text[:MAX_TOOL_RESULT_CHARS]}
    return _parsed(response)


async def _request(
    ctx: ToolContext, method: str, path: str, body: Any = None, *, read_s: float = _READ_S
) -> httpx.Response:
    """One engine call; every failure is a ToolFailure with one of three codes."""
    timeout = httpx.Timeout(connect=5.0, read=read_s, write=10.0, pool=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(method, f"{ctx.base_url}{path}", json=body)
    except httpx.HTTPError as exc:
        raise ToolFailure("engine_unreachable", f"{method} {path} got no answer: {exc!r}") from exc
    if response.status_code == 409 and _parsed(response) == {"client_open": False}:
        raise ToolFailure(
            "no_performance_page",
            "no performance page is open; open /performance in the app first",
        )
    if not response.is_success:
        raise ToolFailure(
            "engine_error", f"{method} {path} -> {response.status_code}: {response.text[:500]}"
        )
    return response


async def _engine(ctx: ToolContext, method: str, path: str, body: Any = None) -> Any:
    return _bounded(await _request(ctx, method, path, body))


def _arg(args: dict[str, Any], name: str, kind: type, *, required: bool = True) -> Any:
    value = args.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, kind):
        raise ToolFailure("invalid_arguments", f"{name} must be a {kind.__name__}, got {value!r}")
    return value


def _library_path(args: dict[str, Any], method: str) -> str:
    path = _arg(args, "path", str)
    try:
        canonical = validate_api_path(path)
        guard_library_request(method, path)
    except SafetyRefusal as exc:
        raise ToolFailure(exc.code, str(exc)) from exc
    if canonical.lower().startswith(_SELF_PREFIX):
        raise ToolFailure("invalid_path", f"the assistant may not call its own routes: {path}")
    return path


def _order(args: dict[str, Any]) -> dict[str, Any]:
    """Exactly one of ``order`` (an AGENT-03 order) or ``verb`` (CLI grammar)."""
    order = _arg(args, "order", dict, required=False)
    verb = _arg(args, "verb", str, required=False)
    if (order is None) == (verb is None):
        raise ToolFailure("invalid_arguments", "give exactly one of order or verb")
    if order is not None:
        return order
    try:
        return single(parse_invocation(shlex.split(verb)))
    except (InvocationError, ValueError) as exc:
        raise ToolFailure("invalid_arguments", str(exc)) from exc


async def read_audible(ctx: ToolContext) -> bool:
    """Is any deck playing or audible right now (no open page = not audible).

    The mirror arrives over HTTP, so this cannot age it: the engine's monotonic
    arrival stamp is private to its process and GET strips it. The served
    ``mirror_stale`` is the engine's own verdict on that stamp (CLOUDSYNC-14),
    so a dead page's last document reads as silent here exactly as it does for
    the engine's gates, and a document that carries no verdict is not trusted.
    """
    try:
        response = await _request(ctx, "GET", MIRROR_PATH)
    except ToolFailure as exc:
        if exc.code == "no_performance_page":
            return False
        raise
    mirror = _parsed(response)
    if not isinstance(mirror, Mapping) or mirror.get("mirror_stale") is not False:
        return False
    return mirror_decks_live(mirror)


# ----- tools --------------------------------------------------------------
async def _status(ctx: ToolContext, _args: dict[str, Any]) -> Any:
    return {
        "health": await _engine(ctx, "GET", "/api/v1/health"),
        "build_info": await _engine(ctx, "GET", "/api/v1/build-info"),
    }


async def _app_state(ctx: ToolContext, _args: dict[str, Any]) -> Any:
    return await _engine(ctx, "GET", MIRROR_PATH)


async def _library_read(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return await _engine(ctx, "GET", _library_path(args, "GET"))


async def _library_write(ctx: ToolContext, args: dict[str, Any]) -> Any:
    method = _arg(args, "method", str).upper()
    if method not in _WRITE_METHODS:
        raise ToolFailure("invalid_arguments", f"method must be one of {_WRITE_METHODS}, got {method}")
    path = _library_path(args, method)
    return await _engine(ctx, method, path, _arg(args, "body", dict, required=False))


def require_steps_succeeded(result: Any) -> Any:
    """A 200 from /commands still carries per-step failures; any non-succeeded step fails the call.

    So does an answer with no steps: nothing ran, so nothing proves the order happened.
    """
    steps = result.get("steps") if isinstance(result, dict) else None
    if not isinstance(steps, list) or not steps:
        raise ToolFailure("engine_error", f"{COMMANDS_PATH} returned no per-step result: {result!r}"[:500])
    failed = [step for step in steps if step.get("status") != "succeeded"]
    if failed:
        errors = "; ".join(str(step.get("error") or step.get("status")) for step in failed)
        message = errors if len(steps) == 1 else f"{len(failed)} of {len(steps)} steps did not run: {errors}"
        raise ToolFailure("command_failed", message)
    return result


async def _command_read_s(ctx: ToolContext, order: dict[str, Any]) -> float:
    """The page holds a ramp's request open for the whole move, so size it as the CLI does."""
    ramp = order.get("ramp")
    over = ramp.get("over") if isinstance(ramp, dict) else None
    if not isinstance(over, dict):
        return _READ_S
    mirror = _parsed(await _request(ctx, "GET", MIRROR_PATH))
    try:
        return ramp_deadline_s(over, _READ_S, mirror)
    except (KeyError, TypeError, ValueError) as exc:
        raise ToolFailure("invalid_arguments", f"cannot size this ramp's deadline: {exc!r}") from exc


async def _command(ctx: ToolContext, args: dict[str, Any]) -> Any:
    order = _order(args)
    response = await _request(ctx, "POST", COMMANDS_PATH, order, read_s=await _command_read_s(ctx, order))
    require_steps_succeeded(_parsed(response))
    return _bounded(response)


_PATH = {"type": "string", "description": "An /api/v1/... path, query string allowed."}

CORE_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="status",
        description="Engine health (state.db counts, version) and build identity.",
        parameters={"type": "object", "properties": {}},
        gate="read",
        run=_status,
    ),
    ToolSpec(
        name="app_state",
        description=(
            "What the open performance page shows right now: decks, loaded tracks, "
            "playing/audible, mixer, toasts. Fails no_performance_page when none is open."
        ),
        parameters={"type": "object", "properties": {}},
        gate="read",
        run=_app_state,
    ),
    ToolSpec(
        name="library_read",
        description=(
            "GET any engine /api/v1 route, e.g. /api/v1/tracks?limit=20&fields=stable_id,"
            "title,artist,bpm,key or /api/v1/playlists. Large answers are truncated; page "
            "with limit/cursor."
        ),
        parameters={"type": "object", "properties": {"path": _PATH}, "required": ["path"]},
        gate="read",
        run=_library_read,
    ),
    ToolSpec(
        name="library_write",
        description=(
            "POST/PUT/PATCH/DELETE an engine /api/v1 route with a JSON body. Rekordbox "
            "writeback and destructive calls are refused. The user approves each call."
        ),
        parameters={
            "type": "object",
            "properties": {
                "method": {"type": "string", "enum": list(_WRITE_METHODS)},
                "path": _PATH,
                "body": {"type": "object"},
            },
            "required": ["method", "path"],
        },
        gate="confirm",
        run=_library_write,
    ),
    ToolSpec(
        name="command",
        description=(
            "Run one performance command on the open page. Give verb in CLI grammar "
            "(e.g. 'play 2', 'deck 1 eq low 0.5') or order as an AGENT-03 object "
            "(single/sequence/parallel/ramp). Verbs: " + ", ".join(sorted(VERBS)) + ". "
            "The user approves each call."
        ),
        parameters={
            "type": "object",
            "properties": {
                "verb": {"type": "string"},
                "order": {"type": "object"},
            },
        },
        gate="confirm",
        run=_command,
    ),
)

__all__ = ["CORE_TOOLS", "MAX_TOOL_RESULT_CHARS", "read_audible"]
