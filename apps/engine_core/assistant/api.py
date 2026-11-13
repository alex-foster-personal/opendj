"""The assistant's HTTP surface: a model proxy (/chat) and a tool loop (/agent).

The first run of Open DJ is a long wait: rekordbox is being snapshotted,
decrypted and ingested while the user watches a progress bar. /chat is the
chat that sits beside that bar. Both routes are stateless, deliberately: the
engine holds no conversation state and stores no history; the client resends it.
Refresh the page and the conversation is gone, because the alternative is
a chat log in state.db that nobody asked for.

    GET  /api/v1/assistant/status   is an upstream configured, and which model
    POST /api/v1/assistant/instant/preview   would a draft run with no model
    POST /api/v1/assistant/chat     stream one completion
    POST /api/v1/assistant/agent    the tool-using loop (agent.py), NDJSON events

The upstream is the user's own OpenRouter key or the Open DJ gateway, never
both and never a fallback from one to the other (upstream.py).

AGENT-NATIVE PARITY: both are plain curl. The system prompt lives HERE
rather than in the sidebar, so an agent posting to /chat gets the same
assistant a human gets -- a browser-only prompt would make the two surfaces
disagree about what the model was told.

    curl -N localhost:8682/api/v1/assistant/chat \\
      -H 'content-type: application/json' \\
      -d '{"messages":[{"role":"user","content":"what is importing?"}]}'

HOUSE RULE, restated because this is the file where it would be easiest to
break: an assistant with no API key returns 409 and stops. It does not fall
back to a second provider, replay a canned answer, or stream an empty
success. A confident sentence with nothing behind it is the worst failure
this app can produce, so the only unconfigured behaviour is a refusal that
names the missing environment variable.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from apps.engine_core.assistant.agent import Turn, complete, held_calls, run_agent
from apps.engine_core.assistant.agent_contract import (
    AGENT_MEDIA_TYPE,
    AGENT_ROUTE,
    AgentIn,
    ChooseTier,
    ProviderCatalogHit,
    ProviderCatalogHitLabel,
    ToolContext,
    ToolFailure,
    ToolSpec,
)
from apps.engine_core.assistant.agent import PROVIDER_CATALOG_PROMPT
from apps.engine_core.assistant.core_tools import (
    ASSISTANT_PROVIDER_FIXTURE_TRANSCRIPTS,
    label_catalog_constrained_hit,
)
from apps.plugins.providers import PARENT_REVIEW_REQUIRED_IDS
from apps.engine_core.assistant.core_tools import CORE_TOOLS, read_audible
from apps.engine_core.assistant.machine_tools import MACHINE_TOOLS
from apps.engine_core.assistant.oj_tools import OJ_TOOLS
from apps.engine_core.assistant.tiers import (
    DEFAULT_MODEL,
    MODEL_ENV,
    Intent,
    TierModels,
    apply_override,
    choose_tier,
)
from apps.engine_core.assistant.upstream import Upstream, UpstreamMode, UpstreamUnconfigured, resolve_upstream

router = APIRouter(prefix="/assistant", tags=["assistant"])

# ----- configuration ------------------------------------------------------
#: Every tool the agent may call: engine tools, machine tools, then the typed
#: OJ tools (one module per domain under oj_tools/, collected there).
ALL_TOOLS: tuple[ToolSpec, ...] = CORE_TOOLS + MACHINE_TOOLS + OJ_TOOLS

#: Refusal codes. The frontend branches on these, so they are values, not
#: prose: `{"detail": {"code", "message"}}` is this repo's error envelope.
#: KEY_MISSING_CODE (409) and its siblings come from upstream.py; the
#: agent's terminal error event code, UPSTREAM_ERROR_CODE, from agent.py.
BAD_HISTORY_CODE: str = "assistant_bad_history"

#: Longest draft /instant/preview checks; every instant command is far shorter.
MAX_PREVIEW_CHARS: int = 2000

#: Unspecified bind addresses are not dialable on every platform (Windows
#: refuses a connect to 0.0.0.0), so a tool reaches the engine on loopback.
_DIAL_HOST: dict[str, str] = {"0.0.0.0": "127.0.0.1", "::": "::1"}

#: What the model is told it is. Short and factual on purpose: a long
#: persona would encourage it to perform expertise about a library it cannot
#: see. The last sentence is the load-bearing one.
SETUP_ASSISTANT_SYSTEM_PROMPT: str = (
    "You are the Open DJ setup assistant. Open DJ is a local-first DJ "
    "library tool, and the user's library import is running in the "
    "background right now while they read this. Answer questions about DJ "
    "libraries, rekordbox, and this app honestly and briefly. You cannot "
    "see the user's library, their files, or how far the import has got. "
    "When you do not know something, say you do not know rather than "
    "guessing."
)


# ----- schemas ------------------------------------------------------------
class ChatMessage(BaseModel):
    """One turn. Same three roles OpenRouter accepts, no tool calls."""

    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1)


class ChatIn(BaseModel):
    """A whole conversation, resent every turn -- the engine stores none of it."""

    messages: list[ChatMessage] = Field(min_length=1)


class AssistantStatusOut(BaseModel):
    """Enough for the sidebar to render an honest panel without a request.

    Firing a chat to discover there is no key would cost a round trip and
    show the user a failed message they did not send.
    """

    configured: bool
    model: str
    #: Which upstream answers: the user's own key or the Open DJ gateway.
    mode: UpstreamMode | None = None
    #: Why not configured (the refusal code /chat and /agent would return).
    code: str | None = None


class InstantPreviewIn(BaseModel):
    text: str = Field(max_length=MAX_PREVIEW_CHARS)


class InstantPreviewOut(BaseModel):
    """Whether ``text`` is an instant command, which /agent answers with no model or key."""

    instant: bool
    #: The rule that matched, e.g. ``rule: play command``; None when not instant.
    reason: str | None = None


class ProviderPolicyOut(BaseModel):
    catalog_prompt: str
    parent_review_required: list[str]
    fixture_transcripts: list[dict[str, str]]


# ----- helpers ------------------------------------------------------------
def _model() -> str:
    return os.environ.get(MODEL_ENV, "").strip() or DEFAULT_MODEL


def _upstream() -> Upstream:
    """The configured upstream, or the 409 refusal naming what is missing."""
    try:
        return resolve_upstream()
    except UpstreamUnconfigured as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "message": exc.message},
        ) from exc


def _own_origin(request: Request) -> str:
    """This engine's origin, from the socket the request arrived on.

    Never the Host header: that is client-controlled, and a tool must not be
    steerable to another origin by whoever sent the request.
    """
    server = request.scope.get("server")
    if not server or server[1] is None:
        raise HTTPException(status_code=500, detail="the engine cannot see its own bound socket")
    host = _DIAL_HOST.get(server[0], server[0])
    # nosemgrep: python.flask.security.audit.directly-returned-format-string.directly-returned-format-string
    return f"http://{f'[{host}]' if ':' in host else host}:{server[1]}"


def _wire_messages(messages: list[ChatMessage]) -> list[dict[str, str]]:
    """The conversation as OpenRouter wants it, system prompt in front.

    A caller that supplies its own leading system message keeps it -- the
    endpoint is scriptable, and silently shadowing a caller's prompt would
    make it un-debuggable.
    """
    wire = [message.model_dump() for message in messages]
    if wire[0]["role"] == "system":
        return wire
    return [{"role": "system", "content": SETUP_ASSISTANT_SYSTEM_PROMPT}, *wire]


async def _stream_text(target: Upstream, messages: list[dict[str, str]]) -> AsyncIterator[bytes]:
    """Upstream's SSE, flattened to the plain text the sidebar appends.

    Decoded here rather than forwarded verbatim so the browser does not have
    to carry an SSE parser and a JSON decoder to render a paragraph. Any
    upstream failure, including a refusal before the first token, aborts the
    chunked response, which every HTTP client surfaces as a broken read --
    deliberately louder than a truncated 200.
    """
    async for delta in complete(target, _model(), messages, (), Turn()):
        yield delta.encode("utf-8")


# ----- routes -------------------------------------------------------------
@router.get("/status", response_model=AssistantStatusOut)
async def assistant_status() -> AssistantStatusOut:
    """Whether a chat can work at all, and which model would answer it."""
    try:
        upstream = resolve_upstream()
    except UpstreamUnconfigured as exc:
        return AssistantStatusOut(configured=False, model=_model(), code=exc.code)
    return AssistantStatusOut(configured=True, model=_model(), mode=upstream.mode)


@router.get("/provider-policy", response_model=ProviderPolicyOut)
async def assistant_provider_policy() -> ProviderPolicyOut:
    """Catalog-constraint rules and fixture transcripts for the agent sidebar."""
    return ProviderPolicyOut(
        catalog_prompt=PROVIDER_CATALOG_PROMPT,
        parent_review_required=sorted(PARENT_REVIEW_REQUIRED_IDS),
        fixture_transcripts=[dict(row) for row in ASSISTANT_PROVIDER_FIXTURE_TRANSCRIPTS],
    )


@router.post("/label-provider-hit", response_model=ProviderCatalogHitLabel)
async def assistant_label_provider_hit(payload: ProviderCatalogHit) -> ProviderCatalogHitLabel:
    """Label a production-library or intelligence hit for assistant presentation."""
    try:
        return label_catalog_constrained_hit(payload.model_dump())
    except ToolFailure as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": exc.code, "message": exc.message},
        ) from exc


@router.post("/instant/preview", response_model=InstantPreviewOut)
async def assistant_instant_preview(payload: InstantPreviewIn) -> InstantPreviewOut:
    """Whether a draft would run as an instant command, asked before Send.

    The same rules /agent applies, so a keyless composer can say up front
    which drafts work without a model, and the grammar lives only in tiers.py.
    """
    intent = Intent.of_text(payload.text)
    return InstantPreviewOut(instant=intent is not None, reason=intent.reason if intent else None)


@router.post(
    "/chat",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"text/plain": {}},
            "description": "The completion, streamed as plain UTF-8 text.",
        },
        409: {"description": "No upstream is configured, or it is ambiguous (detail.code)."},
    },
)
async def assistant_chat(payload: ChatIn) -> StreamingResponse:
    """Stream one completion from the upstream, or explain why there is none.

    A missing or ambiguous upstream is refused BEFORE the response starts, as
    a real 409 with a code. Anything the upstream itself does wrong arrives
    after the 200 and breaks the stream (see ``_stream_text``).
    """
    target = _upstream()
    return StreamingResponse(
        _stream_text(target, _wire_messages(payload.messages)),
        media_type="text/plain; charset=utf-8",
        # Nothing between here and the browser may buffer the body: a
        # buffered stream arrives as one block and the whole point of
        # streaming -- the wait feeling shorter -- is lost.
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


# ----- agent plug-ins (tests swap these via dependency_overrides) ----------
def get_choose_tier() -> ChooseTier:
    return choose_tier


def get_agent_tools() -> tuple[ToolSpec, ...]:
    return ALL_TOOLS


@router.post(
    AGENT_ROUTE,
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {AGENT_MEDIA_TYPE: {}},
            "description": "One AgentEvent JSON object per line (agent_contract.py).",
        },
        409: {"description": "No upstream is configured, or it is ambiguous (detail.code)."},
        422: {"description": f"The history is not one this endpoint streamed ({BAD_HISTORY_CODE})."},
        502: {"description": "The engine could not read its own performance state."},
    },
)
async def assistant_agent(
    payload: AgentIn,
    request: Request,
    choose_tier: ChooseTier = Depends(get_choose_tier),  # noqa: B008  # FastAPI DI
    tools: tuple[ToolSpec, ...] = Depends(get_agent_tools),  # FastAPI DI
) -> StreamingResponse:
    """Run the tool-using loop, streaming NDJSON events.

    Everything that can refuse without a model call refuses here, before the
    stream opens, in the same envelope /chat uses. Once the stream is open an
    upstream failure is a terminal ``error`` event instead.
    """
    try:
        held = held_calls(payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": BAD_HISTORY_CODE, "message": str(exc)},
        ) from exc
    choice = apply_override(choose_tier(payload.messages), payload.tier, TierModels.from_env(os.environ))
    upstream = None if choice.intent is not None else _upstream()
    base_url = _own_origin(request)
    try:
        audible = await read_audible(ToolContext(base_url=base_url, audible=False))
    except ToolFailure as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    return StreamingResponse(
        run_agent(
            payload,
            held=held,
            choice=choice,
            upstream=upstream,
            tools=tools,
            ctx=ToolContext(base_url=base_url, audible=audible),
        ),
        media_type=AGENT_MEDIA_TYPE,
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
