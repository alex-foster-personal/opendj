"""The assistant's own-engine tools: status, app state, library, commands.

Every tool reaches the engine through its public ``/api/v1`` routes at
``ToolContext.base_url`` and nothing else, so the assistant can do exactly
what an HTTP client (curl, ``opendj``, the MCP server) can do and no more.
One public helper, :func:`engine_call`, makes every call (the typed OJ tools
reuse it, and :func:`engine_json` when they project rows before the model sees
them) and is the only place an HTTP failure becomes a :class:`ToolFailure`
code:

- ``engine_unreachable``: the request never got an answer.
- ``no_performance_page``: the documented ``409 {"client_open": false}``.
- ``engine_error``: any other non-2xx, with the engine's own words.

The library tools apply the MCP server's rails (``validate_api_path``,
``guard_library_request``), which judge the canonical path the engine routes,
while the request carries the path as given. ``library_read`` hands back the
engine's ``ETag`` when the answer carries one, and ``library_write`` sends it as
``If-Match`` (the only header a tool may set), so the model can read a playlist
and then rename or reorder it. Every write also carries ``X-Undo-Session:
OJ_UNDO_SESSION``, as each web UI window does, so the library-organization
routes that require it (My Tags, tag types, sections, ``POST /api/v1/undo``)
answer OJ and its edits share one undo history; routes that do not read the
header ignore it. The ``command`` tool applies the destructive switch
(``guard_destructive_commands``) but NOT ``guard_order``, whose other effect is
to prepend a master mute to an audible play, which is right for an unattended
MCP agent and wrong here, where the user approves every command and a hidden
mute would make "play deck 2" silent. Order shape is validated by
``POST /api/v1/commands`` itself (422).
"""

from __future__ import annotations

import shlex
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from apps.engine_core.assistant.agent_contract import (
    ProviderCatalogHit,
    ProviderCatalogHitLabel,
    ToolContext,
    ToolFailure,
    ToolSpec,
)
from apps.opendj_cli.catalog import VERBS
from apps.opendj_cli.mcp_safety import (
    SafetyRefusal,
    guard_destructive_commands,
    guard_library_request,
    validate_api_path,
)
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
#: The undo stack OJ's writes land on: one per engine process, as the web UI
#: takes one per window (``works-overlay.svelte.ts``), so undo never crosses a
#: relaunch and ``POST /api/v1/undo`` from OJ steps back only OJ's own edits.
OJ_UNDO_SESSION: str = f"oj-{uuid.uuid4()}"
#: The assistant must not drive itself: a library call into its own routes
#: would spend a second model turn per tool call.
_SELF_PREFIX: str = "/api/v1/assistant"


# ----- helpers ------------------------------------------------------------
@dataclass(frozen=True)
class EngineAnswer:
    """One engine answer as a tool sees it: the bounded body and the ETag header, if any."""

    #: The answer for the model, as :func:`_bounded` cuts it.
    body: Any
    #: The engine's ``ETag`` header verbatim (quotes kept), or None when it sent none.
    etag: str | None


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
    ctx: ToolContext,
    method: str,
    path: str,
    body: Any = None,
    *,
    if_match: str | None = None,
    read_s: float = _READ_S,
) -> httpx.Response:
    """One engine call; every failure is a ToolFailure with one of three codes."""
    timeout = httpx.Timeout(connect=5.0, read=read_s, write=10.0, pool=5.0)
    headers = {"X-Undo-Session": OJ_UNDO_SESSION} if method.upper() in _WRITE_METHODS else {}
    if if_match is not None:
        headers["If-Match"] = if_match
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(method, f"{ctx.base_url}{path}", json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise ToolFailure("engine_unreachable", f"{method} {path} got no answer: {exc!r}") from exc
    if response.status_code == 409 and _parsed(response) == {"client_open": False}:
        raise ToolFailure(
            "no_performance_page",
            "no performance page is open; open /performance in the app first",
        )
    if not response.is_success:
        raise ToolFailure("engine_error", f"{method} {path} -> {response.status_code}: {response.text[:500]}")
    return response


async def engine_call(
    ctx: ToolContext, method: str, path: str, body: Any = None, *, if_match: str | None = None
) -> EngineAnswer:
    """Call one engine route (``If-Match`` optional) and return its bounded body and ETag.

    A write also sends ``X-Undo-Session: OJ_UNDO_SESSION`` (see the module docstring).

    Fails with a :class:`ToolFailure` (``engine_unreachable``,
    ``no_performance_page`` or ``engine_error``) exactly as every core tool does.
    """
    response = await _request(ctx, method, path, body, if_match=if_match)
    return EngineAnswer(body=_bounded(response), etag=response.headers.get("ETag"))


async def engine_json(ctx: ToolContext, method: str, path: str, body: Any = None) -> Any:
    """Call one engine route and return its WHOLE parsed answer, for a typed tool to project.

    Same failures as :func:`engine_call`, but nothing is cut: the caller owns the
    size of what it hands the model (a typed tool keeps a few fields per row).
    A write carries OJ's undo session as :func:`engine_call` does.
    """
    return _parsed(await _request(ctx, method, path, body))


async def _engine(ctx: ToolContext, method: str, path: str, body: Any = None) -> Any:
    return (await engine_call(ctx, method, path, body)).body


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
    if order is None:
        try:
            order = single(parse_invocation(shlex.split(verb)))
        except (InvocationError, ValueError) as exc:
            raise ToolFailure("invalid_arguments", str(exc)) from exc
    try:
        guard_destructive_commands(order)
    except SafetyRefusal as exc:
        raise ToolFailure(exc.code, str(exc)) from exc
    return order


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
    answer = await engine_call(ctx, "GET", _library_path(args, "GET"))
    if answer.etag is None:
        return answer.body
    return {"etag": answer.etag, "body": answer.body}


async def _library_write(ctx: ToolContext, args: dict[str, Any]) -> Any:
    method = _arg(args, "method", str).upper()
    if method not in _WRITE_METHODS:
        raise ToolFailure("invalid_arguments", f"method must be one of {_WRITE_METHODS}, got {method}")
    path = _library_path(args, method)
    body = _arg(args, "body", dict, required=False)
    if_match = _arg(args, "if_match", str, required=False)
    return (await engine_call(ctx, method, path, body, if_match=if_match)).body


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

#: Fixture transcripts for catalog-constrained assistant replies (issue #11256).
ASSISTANT_PROVIDER_FIXTURE_TRANSCRIPTS: tuple[dict[str, str], ...] = (
    {
        "provider": "epidemic-sound",
        "provider_label": "Epidemic Sound",
        "catalog_constraint": "partner API catalog",
        "transcript": (
            "Epidemic Sound catalog (partner API catalog): Midnight Drive -- "
            "not in your owned library; license through Epidemic before importing."
        ),
    },
    {
        "provider": "soundstripe",
        "provider_label": "Soundstripe",
        "catalog_constraint": "Supe free-account browse",
        "transcript": (
            "Soundstripe catalog (Supe free-account browse): Warm Analog Pad -- "
            "not in your owned library; browse only until you add it locally."
        ),
    },
)


def label_catalog_constrained_hit(raw: dict[str, Any]) -> ProviderCatalogHitLabel:
    """Label a provider catalog hit for the assistant; owned-library hints are refused."""
    hit = ProviderCatalogHit.model_validate(raw)
    if hit.in_owned_library:
        raise ToolFailure(
            "production_library_mislabeled",
            "production-library hits must not be presented as tracks in the owned library",
        )
    if not hit.provider.strip() or not hit.catalog_constraint.strip():
        raise ToolFailure(
            "provider_label_missing",
            "provider name and catalog constraint are required on intelligence hits",
        )
    assistant_label = (
        f"{hit.provider_label} catalog ({hit.catalog_constraint}): {hit.title} -- not in your owned library"
    )
    return ProviderCatalogHitLabel(
        provider=hit.provider,
        provider_label=hit.provider_label,
        catalog_constraint=hit.catalog_constraint,
        title=hit.title,
        in_owned_library=False,
        assistant_label=assistant_label,
    )


def transcript_labels_catalog_hit(
    transcript: str,
    *,
    provider: str,
    catalog_constraint: str,
    provider_label: str | None = None,
) -> None:
    """Refuse transcripts that omit provider or catalog constraint labeling."""
    lowered = transcript.lower()
    provider_names = {provider.lower()}
    if provider_label:
        provider_names.add(provider_label.lower())
    if not any(name in lowered for name in provider_names):
        raise ToolFailure("provider_label_missing", f"transcript omits provider {provider!r}")
    if catalog_constraint.lower() not in lowered:
        raise ToolFailure(
            "catalog_constraint_missing",
            f"transcript omits catalog constraint {catalog_constraint!r}",
        )
    if "owned library" in lowered and "not in" not in lowered:
        raise ToolFailure(
            "production_library_mislabeled",
            "transcript must not imply the production-library hit is already owned",
        )


async def _label_provider_hit(_ctx: ToolContext, args: dict[str, Any]) -> Any:
    return label_catalog_constrained_hit(args).model_dump()


_CORE_TOOLS_WITHOUT_LABEL: tuple[ToolSpec, ...] = (
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
            "with limit/cursor. When the engine sends an ETag the answer is "
            "{etag, body}: pass etag as library_write's if_match."
        ),
        parameters={"type": "object", "properties": {"path": _PATH}, "required": ["path"]},
        gate="read",
        run=_library_read,
    ),
    ToolSpec(
        name="library_write",
        description=(
            "POST/PUT/PATCH/DELETE an engine /api/v1 route with a JSON body. Rekordbox "
            "writeback and destructive calls are refused. Routes that need If-Match "
            "(playlist rename, reorder, membership) take if_match: the etag from "
            "library_read of that resource. Every write carries OJ's own undo session, so "
            "POST /api/v1/undo steps back OJ's last library-organization edit. The user "
            "approves each call."
        ),
        parameters={
            "type": "object",
            "properties": {
                "method": {"type": "string", "enum": list(_WRITE_METHODS)},
                "path": _PATH,
                "body": {"type": "object"},
                "if_match": {
                    "type": "string",
                    "description": "Sent as the If-Match header: the etag library_read returned.",
                },
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

_CORE_TOOLS: tuple[ToolSpec, ...] = _CORE_TOOLS_WITHOUT_LABEL + (
    ToolSpec(
        name="label_provider_hit",
        description=(
            "Label a music-intelligence or production-library catalog hit with "
            "provider name, catalog constraint, and an explicit not-owned-library note."
        ),
        parameters={
            "type": "object",
            "properties": {
                "provider": {"type": "string"},
                "provider_label": {"type": "string"},
                "catalog_constraint": {"type": "string"},
                "title": {"type": "string"},
                "in_owned_library": {"type": "boolean"},
            },
            "required": ["provider", "provider_label", "catalog_constraint", "title"],
        },
        gate="read",
        run=_label_provider_hit,
    ),
)

CORE_TOOLS = _CORE_TOOLS

__all__ = [
    "ASSISTANT_PROVIDER_FIXTURE_TRANSCRIPTS",
    "CORE_TOOLS",
    "MAX_TOOL_RESULT_CHARS",
    "OJ_UNDO_SESSION",
    "EngineAnswer",
    "engine_call",
    "engine_json",
    "label_catalog_constrained_hit",
    "read_audible",
    "transcript_labels_catalog_hit",
]
