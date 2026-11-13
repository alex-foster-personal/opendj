"""Server-side safety rails for ``opendj mcp``.

Play orders require master mute first unless the agent session is live,
writeback HTTP surfaces are refused, and destructive library verbs stay gated
unless ``OPENDJ_MCP_ENABLE_DESTRUCTIVE=1``.

Agent audio is one switch per session (AGENT-25): ``OPENDJ_AGENT_AUDIO`` is
``muted`` (the default) or ``live``. A muted session never makes a sound: every
order that starts a deck, and every transition plan, mutes the master first. A
live session never mutes: it is an agent DJing to the room, and muting the
master mid-set would cut the deck that is already playing.
"""

from __future__ import annotations

import os
import posixpath
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote

from apps.opendj_cli.orders import SEQUENCE, SINGLE
from apps.shared.rekordbox_writeback import WRITE_SURFACES

ENABLE_DESTRUCTIVE_ENV = "OPENDJ_MCP_ENABLE_DESTRUCTIVE"
AGENT_AUDIO_ENV = "OPENDJ_AGENT_AUDIO"
AGENT_AUDIO_MODES: tuple[str, ...] = ("muted", "live")
# Persona cli-mcp loops (issues #3067, #11277) call only these MCP tools.
PERSONA_MCP_TOOLS: frozenset[str] = frozenset({"status", "app_state", "command", "library"})
UPDATE_APPLY_TOOL = "update_apply"
MASTER_MUTE_VERB_ARGS: dict[str, Any] = {"verb": "master_mute", "args": ["true"]}
#: The safety mute: ``persist: False`` silences this run only. Persisting it
#: would write master_muted into the shared ui-prefs.json, and every browser
#: that loads the app next (including the maintainer's headed one) would start muted.
MASTER_MUTE_COMMAND: dict[str, Any] = {"type": "master_mute", "muted": True, "persist": False}

# Curated destructive library paths (method-sensitive). Tested in unit tests.
_DESTRUCTIVE_PATH_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("DELETE", re.compile(r"^/api/v1/.*")),
    ("POST", re.compile(r"^/api/v1/ingest/")),
    ("POST", re.compile(r"^/api/v1/playlists/[^/]+/tracks$")),
    # LIBM-53: the delete custom method can move the track's audio file to the trash.
    ("POST", re.compile(r"^/api/v1/tracks/[^/]+:delete$")),
)

_WRITEBACK_HTTP_MARKERS: tuple[str, ...] = tuple(
    marker
    for surface in WRITE_SURFACES
    if surface.kind == "http" and surface.gated
    for marker in ("/writeback/apply", "/relocate/", "/usb-export/apply")
    if marker in surface.entrypoint.split(" ", 1)[-1]
)


class SafetyRefusal(ValueError):
    """A safety rail blocked the call before dispatch."""

    def __init__(self, code: str, message: str, **fields: Any) -> None:
        super().__init__(message)
        self.code = code
        self.fields = fields


def destructive_enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return env.get(ENABLE_DESTRUCTIVE_ENV, "").strip() in {"1", "true", "yes", "on"}


def agent_audio_mode(environ: Mapping[str, str] | None = None) -> str:
    """``muted`` or ``live``; any other value is refused, never guessed."""
    env = os.environ if environ is None else environ
    raw = env.get(AGENT_AUDIO_ENV, "").strip().lower()
    if raw == "":
        return "muted"
    if raw not in AGENT_AUDIO_MODES:
        raise SafetyRefusal(
            "agent_audio_invalid",
            f"{AGENT_AUDIO_ENV}={raw!r} is not one of {', '.join(AGENT_AUDIO_MODES)}",
        )
    return raw


def _is_audible_play(command: Mapping[str, Any]) -> bool:
    return command.get("type") == "play" and command.get("playing") is True


def _commands_in_order(order: Mapping[str, Any]) -> list[dict[str, Any]]:
    if SINGLE in order:
        payload = order[SINGLE]
        if isinstance(payload, dict):
            return [payload]
        return []
    for kind in (SEQUENCE, "parallel"):
        payload = order.get(kind)
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
    if "ramp" in order:
        payload = order["ramp"]
        if isinstance(payload, dict):
            command = payload.get("command")
            if isinstance(command, dict):
                return [command]
    return []


def order_starts_audible_play(order: Mapping[str, Any]) -> bool:
    return any(_is_audible_play(command) for command in _commands_in_order(order))


def prepend_master_mute(order: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Return an order with :data:`MASTER_MUTE_COMMAND` immediately before each audible play.

    The page awaits each step of a sequence, so a mute at the front would leave a later play behind an unmute made
    during an earlier slow step (a load); the mute is re-sent right before every play that starts audibly.
    """
    if SINGLE in order:
        return {SEQUENCE: [dict(MASTER_MUTE_COMMAND), order[SINGLE]]}, True
    commands = _commands_in_order(order)
    if not commands:
        raise SafetyRefusal(
            "play_blocked",
            "could not prepend master mute to an order with no commands",
        )
    steps: list[dict[str, Any]] = []
    for command in commands:
        if _is_audible_play(command):
            steps.append(dict(MASTER_MUTE_COMMAND))
        steps.append(command)
    return {SEQUENCE: steps}, True


def _refuse_muted_session_order(order: Mapping[str, Any]) -> None:
    """A muted session keeps the master closed: an unmute would make a running or later transition audible, and a raw
    ``finish_now`` plays a live-scheduled plan's remaining steps at once with no master mute (AGENT-25, as DjService.amend)."""
    amend = order.get("plan_amend")
    finishes_now = isinstance(amend, Mapping) and amend.get("action") == "finish_now"
    if finishes_now:
        raise SafetyRefusal(
            "amend_refused",
            "a muted agent session cannot finish a plan now; set OPENDJ_AGENT_AUDIO=live to allow it",
        )
    if any(command.get("type") == "master_mute" and command.get("muted") is False for command in _commands_in_order(order)):
        raise SafetyRefusal(
            "master_unmute_refused",
            "a muted agent session cannot unmute the master; set OPENDJ_AGENT_AUDIO=live to allow it",
        )


def guard_order(
    order: dict[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Apply play gate; return possibly rewritten order and safety metadata.

    Every audible play gets the run-only mute first. A mirror delta from an earlier order is not consulted: it can report
    the master muted after the operator has unmuted it, so skipping the mute on its word would let a play sound.
    """
    if "plan" in order:
        # A plan loads its incoming track, and only schedule_plan holds the schedule lock across that load. A raw
        # plan here could race another schedule's load and replace the track the running plan is about to use.
        raise SafetyRefusal(
            "plan_use_schedule",
            "a plan runs only through schedule_plan, which serializes its track load; a raw plan is refused",
        )
    if agent_audio_mode(environ) == "live":
        return order, None
    _refuse_muted_session_order(order)
    if not order_starts_audible_play(order):
        return order, None
    rewritten, prepended = prepend_master_mute(order)
    return rewritten, {"prepended_master_mute": prepended}


def canonical_api_path(path: str) -> str:
    """The route ``path`` names, for a rail to judge: no query or fragment, decoded once, one slash, no dot segments.

    The engine decodes before routing and the client resolves dot segments, so
    a rail that reads the raw string judges a different path from the one that
    runs. A trailing slash stays: ``/api/v1/ingest/`` is not ``/api/v1/ingest``.
    """
    decoded = unquote(re.split(r"[?#]", path, maxsplit=1)[0])
    canonical = "/" + posixpath.normpath(decoded).lstrip("/")
    return f"{canonical}/" if decoded.endswith(("/", "/.", "/..")) and canonical != "/" else canonical


def _is_sets_path(path: str) -> bool:
    return path == "/api/sets" or path.startswith("/api/sets/")


def _is_recorder_lifecycle_path(path: str) -> bool:
    return path == "/api/sets/recorder/start" or re.fullmatch(
        r"/api/sets/recorder/\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}(?:_\d+)?/(?:stop|recover)",
        path,
    ) is not None


def validate_api_path(path: str, *, method: str | None = None) -> str:
    """Allow v1, GET/HEAD sets reads and exact REC lifecycle POSTs; return the canonical path."""
    # Raw AND canonical: the engine decodes %2e%2e to '..' before routing, and
    # the raw form is appended to an origin, so both must stay in one namespace.
    canonical = canonical_api_path(path)
    raw_route = re.split(r"[?#]", path, maxsplit=1)[0]
    v1_path = path.startswith("/api/v1/") and canonical.startswith("/api/v1/")
    sets_read = (
        method is not None
        and method.upper() in {"GET", "HEAD"}
        and _is_sets_path(raw_route)
        and _is_sets_path(canonical)
    )
    recorder_write = (
        method is not None
        and method.upper() == "POST"
        and _is_recorder_lifecycle_path(raw_route)
        and _is_recorder_lifecycle_path(canonical)
    )
    if not (v1_path or sets_read or recorder_write):
        raise SafetyRefusal(
            "invalid_path",
            f"only /api/v1/*, GET/HEAD /api/sets, or recorder lifecycle POST paths are allowed, got: {path}",
            path=path,
        )
    if ".." in unquote(path):
        raise SafetyRefusal(
            "invalid_path",
            f"path traversal is not allowed: {path}",
            path=path,
        )
    return canonical


def guard_update_apply(environ: Mapping[str, str] | None = None) -> None:
    """Refuse to install an update unless the destructive gate is open.

    Applying an update replaces the running app and restarts it, which is the
    widest-blast-radius call this server has, so it sits behind the same gate
    as the destructive library verbs rather than inventing a second one.
    """
    if destructive_enabled(environ):
        return
    raise SafetyRefusal(
        "destructive_blocked",
        "applying an update replaces and restarts the installed app; set "
        f"{ENABLE_DESTRUCTIVE_ENV}=1 to allow it",
    )


def guard_library_request(method: str, path: str, environ: Mapping[str, str] | None = None) -> None:
    upper = method.upper()
    canonical = canonical_api_path(path)
    for marker in _WRITEBACK_HTTP_MARKERS:
        if marker in canonical or marker in path:
            raise SafetyRefusal(
                "writeback_blocked",
                f"writeback surface blocked: {upper} {path}",
                path=path,
            )
    if destructive_enabled(environ):
        return
    for blocked_method, pattern in _DESTRUCTIVE_PATH_PATTERNS:
        if upper == blocked_method and pattern.match(canonical):
            raise SafetyRefusal(
                "destructive_blocked",
                f"destructive library call blocked: {upper} {path}",
                path=path,
                method=upper,
            )


def command_order_from_arguments(arguments: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the AGENT-03 order a MCP ``command`` call names, if it can be parsed."""
    order = arguments.get("order")
    if isinstance(order, dict):
        return order
    verb = arguments.get("verb")
    if not isinstance(verb, str) or not verb:
        return None
    raw_args = arguments.get("args")
    if raw_args is None:
        rest: list[str] = []
    elif isinstance(raw_args, list):
        rest = [str(item) for item in raw_args]
    else:
        return None
    from apps.opendj_cli.orders import single
    from apps.opendj_cli.verbs import InvocationError, parse_invocation

    try:
        return single(parse_invocation([verb, *rest]))
    except InvocationError:
        return None


def command_arguments_start_audible_play(arguments: Mapping[str, Any]) -> bool:
    """True when a persona ``command`` call would start audible output."""
    order = command_order_from_arguments(arguments)
    if order is None:
        return False
    return order_starts_audible_play(order)


def guard_persona_tool(tool: str, *, allow_destructive: bool) -> None:
    """Refuse persona steps that leave the cli-mcp surface or call ``update_apply``."""
    if tool == UPDATE_APPLY_TOOL:
        raise SafetyRefusal(
            "persona_update_apply_forbidden",
            "persona cli-mcp runs never call update_apply; use update_check only",
        )
    if tool not in PERSONA_MCP_TOOLS:
        raise SafetyRefusal(
            "persona_tool_forbidden",
            f"persona cli-mcp runs only call {', '.join(sorted(PERSONA_MCP_TOOLS))}, not {tool!r}",
        )


def guard_persona_library(method: str, path: str, *, allow_destructive: bool) -> None:
    """Refuse destructive library HTTP before the MCP server unless the run opted in."""
    if allow_destructive:
        return
    guard_library_request(method, path)


def engine_error_code(detail: object, status_code: int) -> str:
    """The code a tool error carries for a refused engine call.

    Routes name a refusal ``code``; the write guard names it ``error``. A
    refusal that names neither still gets a code, ``http_<status>``, never None.
    """
    if isinstance(detail, dict):
        for key in ("code", "error"):
            value = detail.get(key)
            if isinstance(value, str) and value:
                return value
    return f"http_{status_code}"
