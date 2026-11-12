"""Stdio MCP server for installed Open DJ (AGENT-11).

Nine tools over lock-file HTTP: ``status``, ``app_state``, ``command``,
``animate_control`` (ANIM-03),
``library``, ``ui_url``, ``open_route``, ``update_check``, and
``update_apply``. Safety rails live in :mod:`mcp_safety`. The AutoDJ tools
(AGENT-29: mix state, track profiles, mix windows, transition plans) are
registered from :mod:`mcp_dj_tools`.

The two update tools are the agent-native half of the updater (AGENT-13,
issue #2942): the same ``check_via_engine`` and ``apply_via_engine`` the
packaged CLI and the module entry point call, so the MCP server cannot
acquire its own idea of what "installed" means.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx

from apps.opendj_cli.animate import animate_order
from apps.opendj_cli.api_cli import HTTP_METHODS, _parse_field, _parse_header, _request_body
from apps.opendj_cli.blocked import annotate_blocked
from apps.opendj_cli.client import (
    MIRROR_TIMEOUT_S,
    ORDER_TIMEOUT_S,
    EngineClient,
    MalformedResult,
    NoPerformancePage,
    OrderRejected,
    OrderTimedOut,
)
from apps.opendj_cli.engine_status import build_engine_status
from apps.opendj_cli.mcp_audio_tools import register_audio_tools
from apps.opendj_cli.mcp_client_errors_tools import register_client_errors_tools
from apps.opendj_cli.mcp_deck_tools import register_deck_tools
from apps.opendj_cli.mcp_dj_tools import register_dj_tools
from apps.opendj_cli.mcp_energy_tools import register_energy_tools
from apps.opendj_cli.mcp_enrichment_tools import register_enrichment_tools
from apps.opendj_cli.mcp_genre_tools import register_genre_tools
from apps.opendj_cli.mcp_key_tools import register_key_tools
from apps.opendj_cli.mcp_list_totals_tools import register_list_totals_tools
from apps.opendj_cli.mcp_offload_tools import register_offload_tools
from apps.opendj_cli.mcp_played_length_tools import register_played_length_tools
from apps.opendj_cli.mcp_playlist_tools import register_playlist_tools
from apps.opendj_cli.mcp_rec_tools import register_rec_tools
from apps.opendj_cli.mcp_recorder_tools import register_recorder_tools
from apps.opendj_cli.mcp_rekordbox_analyse_tools import register_rekordbox_analyse_tools
from apps.opendj_cli.mcp_remote_tools import register_remote_tools
from apps.opendj_cli.mcp_requests_tools import register_requests_tools
from apps.opendj_cli.mcp_safety import (
    SafetyRefusal,
    guard_library_request,
    guard_order,
    validate_api_path,
)
from apps.opendj_cli.mcp_sdk import MCPServer, ToolAnnotations, ToolError
from apps.opendj_cli.mcp_session_tools import register_session_tools
from apps.opendj_cli.mcp_set_tools import register_set_tools
from apps.opendj_cli.mcp_source_flags_tools import register_source_flags_tools
from apps.opendj_cli.mcp_tempo_tools import register_tempo_tools
from apps.opendj_cli.mcp_track_tools import register_track_tools
from apps.opendj_cli.mcp_update_tools import register_update_tools
from apps.opendj_cli.orders import RAMP, ramp_deadline_s, single
from apps.opendj_cli.origin import (
    EngineIdentityMismatch,
    EngineNotRunning,
    EngineOrigin,
    resolve_origin,
    resolve_verified_origin,
    unreachable,
)
from apps.opendj_cli.shell_navigate import REMEDY_VERB, open_shell_route
from apps.opendj_cli.verbs import InvocationError, parse_invocation

SERVER_NAME = "opendj-mcp"
SERVER_VERSION = "0.1.0"
PROBE_TIMEOUT_S = 3.0
UI_URL_NOTE = (
    "Open in browser MCP only; does not move the installed desktop shell window. "
    "Use open_route for shell navigation (AGENT-12). Release builds have no "
    "WebDriver seam (AGENT-07)."
)

# issue #2878: the client-side MCP tool-result budget (Claude Code's default
# MAX_MCP_OUTPUT_TOKENS is 25k). Kept below that so a GET list page still has
# room for status_code/headers/mcp_note overhead.
MCP_LIST_TOKEN_BUDGET = 20_000
_MCP_CHARS_PER_TOKEN = 4  # matches the issue's own measurement (~446857 chars / ~112k tokens)
# The MCP SDK's pretty-printed text content block runs bigger than a compact
# json.dumps of the same structured payload; measured ~1.41x on this tool's
# own responses. Folded into the estimate so the budget bounds the actual
# wire size, not just our own serialization of it.
_MCP_WIRE_OVERHEAD_FACTOR = 1.45
_MCP_MIN_AUTO_LIMIT = 5
_LIBRARY_PROJECTION_QUERY_KEY = "fields"

_READ_ONLY = ToolAnnotations(read_only_hint=True)
_MUTATING = ToolAnnotations(read_only_hint=False)
_DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True)


@dataclass
class _SessionState:
    lock_path: Path | None = None
    safety_notes: dict[str, Any] = field(default_factory=dict)


_state = _SessionState()


def _tool_error(document: dict[str, Any]) -> ToolError:
    """Every error-document return path routes through here (issue #2895).

    ``document`` must carry a machine-readable ``error`` code; it is
    serialized as JSON so the code stays parseable in the ToolError text,
    not flattened into prose.
    """
    assert "error" in document, "error document must carry a machine-readable code"
    return ToolError(json.dumps(document))


def _engine_not_running(error: EngineNotRunning) -> ToolError:
    return _tool_error(
        {
            "error": "engine_not_running",
            "lock_path": str(error.lock_path),
            "message": str(error),
        }
    )


def _engine_identity_mismatch(error: EngineIdentityMismatch) -> ToolError:
    return _tool_error(
        {
            "error": "engine_identity_mismatch",
            "lock_path": str(error.lock_path),
            "lock_boot_id": error.lock_boot_id,
            "health_boot_id": error.health_boot_id,
            "port": error.port,
            "message": str(error),
        }
    )


def _resolve_origin() -> EngineOrigin:
    return resolve_origin(_state.lock_path)


def _resolve_verified_origin() -> EngineOrigin:
    return resolve_verified_origin(_state.lock_path)


def _parse_order_payload(
    order: dict[str, Any] | None,
    verb: str | None,
    args: list[str] | None,
) -> dict[str, Any]:
    if order is not None:
        return order
    if verb is None:
        raise SafetyRefusal(
            "usage",
            "command requires either order or verb",
        )
    tokens = [verb, *(args or [])]
    try:
        invocation = parse_invocation(tokens)
    except InvocationError as error:
        raise SafetyRefusal("usage", str(error)) from error
    return single(invocation)


def _parse_library_fields(raw: dict[str, str] | None) -> tuple[tuple[str, Any], ...]:
    if not raw:
        return ()
    return tuple(_parse_field(f"{key}={value}") for key, value in raw.items())


def _parse_library_headers(raw: dict[str, str] | None) -> tuple[tuple[str, str], ...]:
    if not raw:
        return ()
    return tuple(_parse_header(f"{key}:{value}") for key, value in raw.items())


def _parse_app_state_body(response: httpx.Response) -> Any:
    content_type = response.headers.get("content-type", "")
    if "json" in content_type.lower():
        try:
            return response.json()
        except ValueError:
            pass
    return response.text


#----- library GET pagination + projection (issue #2878) --------------------


def _estimate_mcp_tokens(payload: Any) -> int:
    """Rough MCP wire token count: ~4 chars/token, scaled for the SDK's
    pretty-printed text block (measured heavier than a compact json.dumps)."""
    chars = len(json.dumps(payload)) * _MCP_WIRE_OVERHEAD_FACTOR
    return int(chars // _MCP_CHARS_PER_TOKEN)


def _split_query(path: str) -> tuple[str, list[tuple[str, str]]]:
    split = urlsplit(path)
    return split.path, parse_qsl(split.query, keep_blank_values=True)


def _build_path(base_path: str, query: list[tuple[str, str]]) -> str:
    return base_path if not query else f"{base_path}?{urlencode(query)}"


def _has_query_param(query: list[tuple[str, str]], name: str) -> bool:
    return any(key == name for key, _ in query)


def _extract_projection(
    query: list[tuple[str, str]],
) -> tuple[list[tuple[str, str]], list[str] | None]:
    remaining = [(k, v) for k, v in query if k != _LIBRARY_PROJECTION_QUERY_KEY]
    raw_values = [v for k, v in query if k == _LIBRARY_PROJECTION_QUERY_KEY]
    if not raw_values:
        return remaining, None
    names = [name.strip() for name in raw_values[-1].split(",") if name.strip()]
    return remaining, names or None


def _project_items(items: list[Any], field_names: list[str]) -> list[Any]:
    return [
        {name: item[name] for name in field_names if name in item}
        if isinstance(item, dict)
        else item
        for item in items
    ]


def _is_paginated_list(body: Any) -> bool:
    return (
        isinstance(body, dict)
        and isinstance(body.get("items"), list)
        and "next_cursor" in body
    )


def _shrunk_page_limit(item_count: int, measured_tokens: int, budget: int) -> int:
    if item_count <= 0 or measured_tokens <= 0:
        return _MCP_MIN_AUTO_LIMIT
    shrunk = int(item_count * (budget / measured_tokens) * 0.85)
    return max(_MCP_MIN_AUTO_LIMIT, min(item_count, shrunk))


def _apply_projection(body: Any, projection: list[str] | None) -> Any:
    if projection and isinstance(body, dict) and isinstance(body.get("items"), list):
        return {**body, "items": _project_items(body["items"], projection)}
    return body


def _bound_list_page(
    *,
    path: str,
    base_path: str,
    query: list[tuple[str, str]],
    projection: list[str] | None,
    payload: dict[str, Any],
    parsed_body: dict[str, Any],
    do_request: Callable[[str], httpx.Response],
    origin: EngineOrigin,
) -> dict[str, Any]:
    """Auto-bound an oversized GET list page via a real, smaller-limit retry.

    Never client-side truncates an already-fetched page: an authentic
    engine-computed page (with a real, usable cursor) is fetched instead.
    """
    tokens = _estimate_mcp_tokens(payload)
    if tokens <= MCP_LIST_TOKEN_BUDGET:
        return payload
    if _has_query_param(query, "limit"):
        raise _tool_error(
            {
                "error": "mcp_budget_exceeded",
                "message": (
                    f"library GET {path} is ~{tokens} tokens, over the "
                    f"{MCP_LIST_TOKEN_BUDGET}-token MCP result budget even at "
                    "the limit you passed. Narrow it with a filter "
                    "(q/bpm_min/bpm_max/key/tag), a smaller limit, or "
                    "?fields= to project fewer columns."
                ),
            }
        )
    retry_limit = _shrunk_page_limit(len(parsed_body["items"]), tokens, MCP_LIST_TOKEN_BUDGET)
    retry_path = _build_path(base_path, [*query, ("limit", str(retry_limit))])
    retry_response = _library_do_request(origin, do_request, retry_path)
    retry_body = _apply_projection(_parse_app_state_body(retry_response), projection)
    bounded: dict[str, Any] = {
        "status_code": retry_response.status_code,
        "body": retry_body,
        "mcp_note": (
            f"auto-limited to {retry_limit} items: the unbounded page "
            f"was ~{tokens} tokens, over the {MCP_LIST_TOKEN_BUDGET}-"
            "token MCP result budget. Page the rest with "
            "?cursor=<body.next_cursor> (and the same ?limit), or "
            "narrow with a filter."
        ),
    }
    if retry_response.headers:
        bounded["headers"] = dict(retry_response.headers)
    retry_tokens = _estimate_mcp_tokens(bounded)
    if retry_tokens > MCP_LIST_TOKEN_BUDGET:
        raise _tool_error(
            {
                "error": "mcp_budget_exceeded",
                "message": (
                    f"library GET {path} could not be bounded under "
                    f"{MCP_LIST_TOKEN_BUDGET} tokens even at limit={retry_limit}; "
                    "narrow it with a filter (q/bpm_min/bpm_max/key/tag) or "
                    "?fields=."
                ),
            }
        )
    return bounded


def _library_do_request(
    origin: EngineOrigin,
    do_request: Callable[[str], httpx.Response],
    target_path: str,
) -> httpx.Response:
    try:
        return do_request(target_path)
    except httpx.TransportError as error:
        raise _engine_not_running(unreachable(origin, error)) from error
    except httpx.InvalidURL as error:
        raise _tool_error({"error": "usage", "message": str(error)}) from error
    except httpx.UnsupportedProtocol as error:
        raise _tool_error({"error": "usage", "message": str(error)}) from error


def _post_guarded(
    client: EngineClient, guarded_order: dict[str, Any], safety: dict[str, Any] | None
) -> dict[str, Any]:
    """Post one guarded order and shape the page's result as a tool document."""
    origin = client.origin
    try:
        result = client.post_order(guarded_order)
    except NoPerformancePage as error:
        raise _tool_error(
            {
                "error": "no_performance_page",
                "origin": origin.base_url,
                "remedy": REMEDY_VERB,
                "message": str(error),
            }
        ) from error
    except (OrderRejected, OrderTimedOut, MalformedResult) as error:
        raise _tool_error({"error": "order_failed", "message": str(error)}) from error
    except EngineNotRunning as error:
        raise _engine_not_running(error) from error

    try:
        document = annotate_blocked(dict(result))  # AGENT-55/54
    except MalformedResult as error:
        raise _tool_error({"error": "order_failed", "message": str(error)}) from error
    if safety:
        document["safety"] = safety
    return document


def _animate_control(
    control: str, expr: str, deck: int | None, command_timeout_s: float
) -> dict[str, Any]:
    """ANIM-03: the body of the ``animate_control`` tool."""
    try:
        # Parsed before the engine is looked up, like the CLI: a typo is
        # answered as a typo even with the app closed.
        try:
            order = animate_order(deck, control, expr)
        except InvocationError as error:
            raise SafetyRefusal("usage", str(error)) from error
        origin = _resolve_verified_origin()
        guarded_order, safety = guard_order(order)
    except SafetyRefusal as error:
        raise _tool_error(
            {"error": error.code, "message": str(error), **error.fields}
        ) from error
    except EngineIdentityMismatch as error:
        raise _engine_identity_mismatch(error) from error
    except EngineNotRunning as error:
        raise _engine_not_running(error) from error

    client = EngineClient(origin=origin, timeout_s=command_timeout_s)
    try:
        # The page holds a ramp open for its whole duration, so size the
        # deadline from the live clock exactly as the CLI's --over does.
        client.timeout_s = ramp_deadline_s(order[RAMP]["over"], command_timeout_s, client.mirror())
    except NoPerformancePage as error:
        raise _tool_error(
            {"error": "no_performance_page", "origin": origin.base_url, "remedy": REMEDY_VERB, "message": str(error)}
        ) from error
    except EngineNotRunning as error:
        raise _engine_not_running(error) from error
    return _post_guarded(client, guarded_order, safety)


def create_server(*, command_timeout_s: float = ORDER_TIMEOUT_S) -> MCPServer:
    server = MCPServer(name=SERVER_NAME, version=SERVER_VERSION)

    @server.tool(annotations=_READ_ONLY)
    def status() -> dict[str, Any]:
        """Engine origin from the lock file, health, and build-info."""
        try:
            origin = _resolve_verified_origin()
        except EngineIdentityMismatch as error:
            raise _engine_identity_mismatch(error) from error
        except EngineNotRunning as error:
            raise _engine_not_running(error) from error
        try:
            return build_engine_status(origin)
        except EngineNotRunning as error:
            raise _engine_not_running(error) from error

    @server.tool(annotations=_READ_ONLY, structured_output=True)
    def app_state(path: str = "/api/v1/setup/status") -> dict[str, Any]:
        """GET-only proxy to ``/api/v1/*``, ``/api/sets`` and ``/api/sets/*``."""
        try:
            validate_api_path(path, method="GET")
            origin = _resolve_origin()
        except SafetyRefusal as error:
            raise _tool_error(
                {"error": error.code, "message": str(error), **error.fields}
            ) from error
        except EngineNotRunning as error:
            raise _engine_not_running(error) from error
        url = f"{origin.base_url}{path}"
        try:
            with httpx.Client(timeout=PROBE_TIMEOUT_S) as client:
                response = client.get(url)
        except httpx.TransportError as error:
            raise _engine_not_running(unreachable(origin, error)) from error
        if not response.is_success:
            raise _tool_error(
                {
                    "error": "upstream_error",
                    "status_code": response.status_code,
                    "message": f"engine GET {path} -> {response.status_code}: "
                    f"{response.text[:500]}",
                }
            )
        body = _parse_app_state_body(response)
        if isinstance(body, dict):
            return body
        return {"body": body}

    @server.tool(annotations=_DESTRUCTIVE)
    def command(
        order: dict[str, Any] | None = None,
        verb: str | None = None,
        args: list[str] | None = None,
    ) -> dict[str, Any]:
        """Dispatch one AGENT-03 order and return the page result document.

        A load onto a playing deck is refused: ``verdict: "blocked"`` plus
        ``blocked`` (deck, playing track, ``override_order``/``override_cli``).
        The override is ``load`` with ``replace_playing: true`` (it stops that
        deck, then loads). Send it only if the user's request implied replacing
        a playing deck (e.g. "load four decks now"); if unclear, ask the user first.

        Dial positions (DIALCLOCK-8341) are clock times: 12 o'clock is centre (0.5,
        flat or dry), 9 o'clock is 0.1667, 3 o'clock is 0.8333, and the dial runs
        from 7:30 (0) to 4:30 (1). With ``verb`` + ``args`` the trim, eq, filter
        and color_fx values take "3pm", "3 o'clock", "15:00" or a 0..1 number
        (``1`` is full right; say "1pm" for one o'clock). A raw ``order`` takes
        the 0..1 number only. ``color_fx`` with ``effect=next`` cycles FILTER,
        SPACE, ECHO, CRUSH; switching effect removes the old one and returns the knob
        to 12 o'clock unless ``value`` is given (COLORFX-8341). ``color_fx`` also
        takes ``deck=all`` to put one named effect on every channel; ``value`` and
        ``effect=next`` are refused with ``deck=all`` (COLORFX-13).
        """
        try:
            origin = _resolve_verified_origin()
            parsed_order = _parse_order_payload(order, verb, args)
            guarded_order, safety = guard_order(parsed_order)
        except SafetyRefusal as error:
            raise _tool_error(
                {"error": error.code, "message": str(error), **error.fields}
            ) from error
        except EngineIdentityMismatch as error:
            raise _engine_identity_mismatch(error) from error
        except EngineNotRunning as error:
            raise _engine_not_running(error) from error

        return _post_guarded(
            EngineClient(origin=origin, timeout_s=command_timeout_s), guarded_order, safety
        )

    @server.tool(annotations=_DESTRUCTIVE)
    def animate_control(control: str, expr: str, deck: int | None = None) -> dict[str, Any]:
        """Animate one dial or fader on the beat clock (ANIM-03), like its right-click Animate box.

        ``control``: eq_low, eq_mid, eq_high, trim, filter, fader, crossfader,
        stem_vocal, stem_instrumental, stem_drums or stem_bass. ``deck`` is 1..4,
        omitted for the crossfader. ``expr`` is the CLI ramp tail:
        ``<to 0..1> --over <N>beats|bars|phrases|ms [--anchor next_beat|next_downbeat|next_phrase] [--clock master|1-4]``,
        e.g. ``0 --over 8bars --anchor next_downbeat`` kills EQ LOW over 8 bars from the next downbeat.
        A bad expression is refused before anything is sent, naming the failing token.
        The call returns when the ramp ends. Moving the control by hand, or any set
        command to it (e.g. ``command`` verb ``eq``), stops it (take-over).
        """
        return _animate_control(control, expr, deck, command_timeout_s)

    @server.tool(annotations=_DESTRUCTIVE)
    def library(
        method: str,
        path: str,
        body: str | None = None,
        fields: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Raw library HTTP against the engine origin (LIBM-11).

        ``body`` carries the same JSON string ``opendj api --json`` accepts.
        The name ``json`` is reserved by the MCP wire format, so this tool
        exposes ``body`` instead.

        A GET against a paginated list route (a body shaped like ``items`` +
        ``next_cursor``) is auto-limited to stay under the MCP result budget
        when the caller passes no explicit ``?limit=`` in ``path``; page the
        rest with ``?cursor=<body.next_cursor>``. Add
        ``?fields=stable_id,title,artist,bpm,key`` to a list route's path for
        a compact projection instead of full rows.
        """
        upper = method.upper()
        if upper not in HTTP_METHODS:
            raise _tool_error({"error": "usage", "message": f"unknown HTTP method {method!r}"})
        if not path.startswith("/"):
            raise _tool_error(
                {"error": "usage", "message": f"PATH must start with '/', got {path!r}"}
            )
        try:
            guard_library_request(upper, path)
            origin = _resolve_origin()
            request_body = _request_body(
                upper,
                body,
                _parse_library_fields(fields),
            )
        except SafetyRefusal as error:
            raise _tool_error(
                {"error": error.code, "message": str(error), **error.fields}
            ) from error
        except EngineNotRunning as error:
            raise _engine_not_running(error) from error
        except ValueError as error:
            raise _tool_error({"error": "usage", "message": str(error)}) from error

        base_path, query = _split_query(path)
        query, projection = _extract_projection(query)

        request_headers = {"Accept": "application/json", **dict(_parse_library_headers(headers))}
        if request_body is not None:
            request_headers["Content-Type"] = "application/json"

        def _do_request(target_path: str) -> httpx.Response:
            url = f"{origin.base_url}{target_path}"
            with httpx.Client(timeout=MIRROR_TIMEOUT_S) as client:
                return client.request(
                    upper, url, headers=request_headers, content=request_body
                )

        response = _library_do_request(
            origin, _do_request, _build_path(base_path, query)
        )

        parsed_body = _apply_projection(_parse_app_state_body(response), projection)

        payload: dict[str, Any] = {"status_code": response.status_code, "body": parsed_body}
        if response.headers:
            payload["headers"] = dict(response.headers)

        if upper == "GET" and response.is_success and _is_paginated_list(parsed_body):
            payload = _bound_list_page(
                path=path,
                base_path=base_path,
                query=query,
                projection=projection,
                payload=payload,
                parsed_body=parsed_body,
                do_request=_do_request,
                origin=origin,
            )
        return payload

    @server.tool(annotations=_MUTATING)
    def open_route(route: str = "/performance") -> dict[str, Any]:
        """Navigate the installed desktop shell to a route (AGENT-12)."""
        if not route.startswith("/"):
            route = f"/{route}"
        try:
            origin = _resolve_origin()
        except EngineNotRunning as error:
            raise _engine_not_running(error) from error
        try:
            if route != "/performance" and not route.startswith("/performance/"):
                raise _tool_error(
                    {
                        "error": "usage",
                        "message": f"route {route!r} is not allowlisted; use /performance",
                    }
                )
            result = open_shell_route(origin, route)
        except NoPerformancePage as error:
            raise _tool_error(
                {
                    "error": "no_performance_page",
                    "remedy": REMEDY_VERB,
                    "origin": origin.base_url,
                    "message": str(error),
                }
            ) from error
        except EngineNotRunning as error:
            raise _engine_not_running(error) from error
        return {"accepted": True, **result}

    @server.tool(annotations=_READ_ONLY)
    def ui_url(route: str = "/") -> dict[str, Any]:
        """Return the engine-served SPA URL for browser MCP driving."""
        if not route.startswith("/"):
            route = f"/{route}"
        try:
            origin = _resolve_origin()
        except EngineNotRunning as error:
            raise _engine_not_running(error) from error
        url = f"{origin.base_url}{route}"
        document: dict[str, Any] = {"url": url, "note": UI_URL_NOTE}
        try:
            with httpx.Client(timeout=PROBE_TIMEOUT_S) as client:
                response = client.get(url)
            if response.status_code == 404:
                document["reachable"] = False
            else:
                document["reachable"] = response.is_success
        except httpx.TransportError:
            document["reachable"] = False
        return document

    def _dispatch_order(order: dict[str, Any]) -> dict[str, Any]:
        try:
            origin = _resolve_verified_origin()
            guarded_order, safety = guard_order(order)
        except SafetyRefusal as error:
            raise _tool_error({"error": error.code, "message": str(error), **error.fields}) from error
        except EngineIdentityMismatch as error:
            raise _engine_identity_mismatch(error) from error
        except EngineNotRunning as error:
            raise _engine_not_running(error) from error
        return _post_guarded(EngineClient(origin=origin, timeout_s=command_timeout_s), guarded_order, safety)

    def _read_mirror() -> dict[str, Any]:
        try:
            return EngineClient(origin=_resolve_verified_origin(), timeout_s=command_timeout_s).mirror()
        except NoPerformancePage as error:
            raise _tool_error({"error": "no_performance_page", "remedy": REMEDY_VERB, "message": str(error)}) from error
        except EngineIdentityMismatch as error:
            raise _engine_identity_mismatch(error) from error
        except EngineNotRunning as error:
            raise _engine_not_running(error) from error

    register_key_tools(server, post_order=_dispatch_order, read_mirror=_read_mirror, tool_error=_tool_error)
    from apps.opendj_cli.mcp_replay_tools import register_replay_tools

    register_replay_tools(server, run_order=_dispatch_order, tool_error=_tool_error)
    register_update_tools(
        server,
        lock_path=lambda: _state.lock_path,
        tool_error=_tool_error,
        engine_not_running=_engine_not_running,
    )
    register_deck_tools(server, run_order=lambda order: command(order=order))
    register_dj_tools(
        server,
        lock_path=lambda: _state.lock_path,
        tool_error=_tool_error,
        engine_not_running=_engine_not_running,
        engine_identity_mismatch=_engine_identity_mismatch,
    )
    register_genre_tools(
        server,
        lock_path=lambda: _state.lock_path,
        tool_error=_tool_error,
        engine_not_running=_engine_not_running,
    )
    register_energy_tools(
        server,
        lock_path=lambda: _state.lock_path,
        tool_error=_tool_error,
        engine_not_running=_engine_not_running,
    )
    register_audio_tools(
        server,
        lock_path=lambda: _state.lock_path,
        tool_error=_tool_error,
        engine_not_running=_engine_not_running,
    )
    register_played_length_tools(
        server,
        lock_path=lambda: _state.lock_path,
        tool_error=_tool_error,
        engine_not_running=_engine_not_running,
    )
    register_list_totals_tools(
        server,
        lock_path=lambda: _state.lock_path,
        tool_error=_tool_error,
        engine_not_running=_engine_not_running,
    )
    register_source_flags_tools(
        server,
        lock_path=lambda: _state.lock_path,
        tool_error=_tool_error,
        engine_not_running=_engine_not_running,
    )
    register_client_errors_tools(
        server,
        lock_path=lambda: _state.lock_path,
        tool_error=_tool_error,
        engine_not_running=_engine_not_running,
    )
    register_session_tools(
        server,
        lock_path=lambda: _state.lock_path,
        tool_error=_tool_error,
        engine_not_running=_engine_not_running,
    )
    register_requests_tools(
        server,
        lock_path=lambda: _state.lock_path,
        tool_error=_tool_error,
        engine_not_running=_engine_not_running,
    )
    register_remote_tools(
        server,
        lock_path=lambda: _state.lock_path,
        tool_error=_tool_error,
        engine_not_running=_engine_not_running,
    )
    register_enrichment_tools(
        server,
        lock_path=lambda: _state.lock_path,
        tool_error=_tool_error,
        engine_not_running=_engine_not_running,
    )
    register_rekordbox_analyse_tools(
        server,
        lock_path=lambda: _state.lock_path,
        tool_error=_tool_error,
        engine_not_running=_engine_not_running,
    )
    register_offload_tools(
        server,
        lock_path=lambda: _state.lock_path,
        tool_error=_tool_error,
        engine_not_running=_engine_not_running,
    )
    register_track_tools(
        server,
        lock_path=lambda: _state.lock_path,
        tool_error=_tool_error,
        engine_not_running=_engine_not_running,
    )
    register_recorder_tools(
        server,
        lock_path=lambda: _state.lock_path,
        tool_error=_tool_error,
        engine_not_running=_engine_not_running,
    )
    register_set_tools(
        server,
        lock_path=lambda: _state.lock_path,
        tool_error=_tool_error,
        engine_not_running=_engine_not_running,
    )
    register_rec_tools(
        server,
        lock_path=lambda: _state.lock_path,
        tool_error=_tool_error,
        engine_not_running=_engine_not_running,
    )
    register_tempo_tools(
        server,
        lock_path=lambda: _state.lock_path,
        tool_error=_tool_error,
        engine_not_running=_engine_not_running,
    )
    register_playlist_tools(
        server,
        lock_path=lambda: _state.lock_path,
        tool_error=_tool_error,
        engine_not_running=_engine_not_running,
    )
    return server


def run_stdio(
    lock_path: Path | None = None,
    *,
    command_timeout_s: float = ORDER_TIMEOUT_S,
) -> None:
    """Start the stdio MCP server until stdin closes."""
    _state.lock_path = lock_path
    create_server(command_timeout_s=command_timeout_s).run(transport="stdio")
