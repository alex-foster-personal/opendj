"""The setup assistant's HTTP surface -- an OpenRouter proxy, nothing more.

The first run of Open DJ is a long wait: rekordbox is being snapshotted,
decrypted and ingested while the user watches a progress bar. This is the
chat that sits beside that bar. It is a PROXY, deliberately: the engine
holds no conversation state, runs no prompt chain and stores no history.
Refresh the page and the conversation is gone, because the alternative is
a chat log in state.db that nobody asked for.

    GET  /api/v1/assistant/status   is a key configured, and which model
    POST /api/v1/assistant/chat     stream one completion

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

import json
import os
from collections.abc import AsyncIterator
from typing import Any, Literal

import httpx
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

router = APIRouter(prefix="/assistant", tags=["assistant"])

# ----- configuration ------------------------------------------------------
#: Which model to call. Overridable because the maintainer named two candidates
#: (an OpenRouter-hosted Terra agent and Gemini Flash 3.7) and OpenRouter
#: serves both behind one API -- switching is an env var, not a code change.
MODEL_ENV: str = "MDT_ASSISTANT_MODEL"

#: The credential. Absent = the endpoint refuses; see the module docstring.
OPENROUTER_KEY_ENV: str = "OPENROUTER_API_KEY"

#: Where to send the completion. Exists so the tests can point the proxy at
#: a real local HTTP server instead of the internet.
OPENROUTER_BASE_ENV: str = "MDT_OPENROUTER_BASE_URL"

#: Gemini Flash 3.7's OpenRouter slug, read off
#: https://openrouter.ai/api/v1/models on Wed 19 Aug 2026 -- listed there as
#: "Google: Gemini 3.7 Flash", canonical_slug google/gemini-3.7-flash-20260813,
#: 1,048,576-token context. NOT guessed from the product name: OpenRouter's
#: slugs invert Google's marketing order (gemini-3.7-flash, not
#: gemini-flash-3.7) and a wrong slug is a 404 on every single request.
DEFAULT_MODEL: str = "google/gemini-3.7-flash"

DEFAULT_BASE_URL: str = "https://openrouter.ai/api/v1"

#: No retry policy on purpose. A retried completion is a second charge and a
#: second wait, and the caller -- who can see the error -- is better placed
#: to decide than a hidden loop is.
_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)

#: Refusal codes. The frontend branches on these, so they are values, not
#: prose: `{"detail": {"code", "message"}}` is this repo's error envelope.
KEY_MISSING_CODE: str = "assistant_key_missing"
UPSTREAM_ERROR_CODE: str = "assistant_upstream_error"

KEY_MISSING_MESSAGE: str = (
    f"the assistant has no API key: set {OPENROUTER_KEY_ENV} in the engine's "
    "environment and restart it. Nothing is answered without one -- this "
    "endpoint will not invent a reply."
)

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


# ----- helpers ------------------------------------------------------------
def _model() -> str:
    return os.environ.get(MODEL_ENV, "").strip() or DEFAULT_MODEL


def _base_url() -> str:
    configured = os.environ.get(OPENROUTER_BASE_ENV, "").strip()
    return (configured or DEFAULT_BASE_URL).rstrip("/")


def _api_key() -> str | None:
    """The key, or None. An empty/whitespace value counts as absent.

    `OPENROUTER_API_KEY=` in a .env file is someone clearing the key, not
    setting it to the empty string, and treating it as present would turn a
    clear 409 into a baffling upstream 401.
    """
    return os.environ.get(OPENROUTER_KEY_ENV, "").strip() or None


def _upstream_failure(message: str, upstream_status: int | None) -> HTTPException:
    """502, because this endpoint IS a gateway and the gateway is what failed.

    `upstream_status` rides along as its own field so a caller can branch on
    402-out-of-credit versus 429-rate-limited without parsing the sentence.
    """
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={
            "code": UPSTREAM_ERROR_CODE,
            "message": message,
            "upstream_status": upstream_status,
        },
    )


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


def _delta_text(data: str) -> str:
    """The token text in one SSE data payload, or "" when it carries none.

    Raises rather than skipping on anything unrecognised. A parser that
    shrugged at a malformed chunk would silently drop part of a reply, and a
    reply with a hole in it is worse than no reply.
    """
    try:
        payload: dict[str, Any] = json.loads(data)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"OpenRouter sent a non-JSON SSE payload: {data[:200]!r}"
        ) from exc
    error = payload.get("error")
    if error is not None:
        raise RuntimeError(f"OpenRouter reported an error mid-stream: {error}")
    choices = payload.get("choices") or []
    if not choices:
        # Usage-only and role-only frames are normal and carry no text.
        return ""
    return (choices[0].get("delta") or {}).get("content") or ""


async def _stream_completion(
    client: httpx.AsyncClient, upstream: httpx.Response
) -> AsyncIterator[bytes]:
    """Upstream's SSE, flattened to the plain text the sidebar appends.

    Decoded here rather than forwarded verbatim so the browser does not have
    to carry an SSE parser and a JSON decoder to render a paragraph. Failing
    mid-stream aborts the chunked response, which every HTTP client surfaces
    as a broken read -- deliberately louder than a truncated 200.
    """
    try:
        async for line in upstream.aiter_lines():
            # Blank separators and `: OPENROUTER PROCESSING` keep-alives, plus
            # SSE's own event:/id: fields. All framing, none of it content.
            if not line or line.startswith(":") or not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                return
            text = _delta_text(data)
            if text:
                yield text.encode("utf-8")
    finally:
        await upstream.aclose()
        await client.aclose()


# ----- routes -------------------------------------------------------------
@router.get("/status", response_model=AssistantStatusOut)
async def assistant_status() -> AssistantStatusOut:
    """Whether a chat can work at all, and which model would answer it."""
    return AssistantStatusOut(configured=_api_key() is not None, model=_model())


@router.post(
    "/chat",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"text/plain": {}},
            "description": "The completion, streamed as plain UTF-8 text.",
        },
        409: {"description": f"No {OPENROUTER_KEY_ENV} is configured."},
        502: {"description": "OpenRouter refused or could not be reached."},
    },
)
async def assistant_chat(payload: ChatIn) -> StreamingResponse:
    """Stream one completion from OpenRouter, or explain why there is none.

    The upstream status is checked BEFORE the response starts, so a refusal
    arrives as a real HTTP error with a code rather than as a 200 whose body
    turns out to be empty.
    """
    key = _api_key()
    if key is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": KEY_MISSING_CODE, "message": KEY_MISSING_MESSAGE},
        )

    base = _base_url()
    client = httpx.AsyncClient(timeout=_TIMEOUT)
    request = client.build_request(
        "POST",
        f"{base}/chat/completions",
        json={
            "model": _model(),
            "messages": _wire_messages(payload.messages),
            "stream": True,
        },
        headers={
            "Authorization": f"Bearer {key}",
            # OpenRouter's app-attribution headers; they show which app the
            # spend came from on the account's activity page.
            "X-Title": "Open DJ",
            "HTTP-Referer": "https://github.com/former-work-account/music-dj-tools",
        },
    )
    try:
        upstream = await client.send(request, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise _upstream_failure(
            f"could not reach OpenRouter at {base}: {exc!r}", None
        ) from exc

    if upstream.status_code != 200:
        body = (await upstream.aread()).decode("utf-8", errors="replace")
        await upstream.aclose()
        await client.aclose()
        raise _upstream_failure(
            f"OpenRouter returned {upstream.status_code} for model "
            f"{_model()}: {body[:600]}",
            upstream.status_code,
        )

    return StreamingResponse(
        _stream_completion(client, upstream),
        media_type="text/plain; charset=utf-8",
        # Nothing between here and the browser may buffer the body: a
        # buffered stream arrives as one block and the whole point of
        # streaming -- the wait feeling shorter -- is lost.
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
