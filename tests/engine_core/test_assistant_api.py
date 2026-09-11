"""The assistant chat surface: an OpenRouter proxy that never pretends.

The sidebar assistant is the one place in the app where a plausible-looking
sentence can be manufactured out of nothing, so the tests here are mostly
about REFUSALS rather than replies:

  - if no OPENROUTER_API_KEY is set then /chat must 409 with a code, not
    fall back to a canned reply, a second provider, or an empty stream
  - if the key is absent then /status must say so BEFORE a chat is fired,
    so the UI can render an honest panel without spending a request
  - if OpenRouter answers 4xx/5xx then the caller must see that it did,
    with the upstream status in the message -- a swallowed upstream error
    reads to a user as "the assistant had nothing to say"
  - if OpenRouter streams tokens then exactly those tokens must arrive at
    the client, in order

The upstream in these tests is a REAL HTTP server (uvicorn on an ephemeral
loopback port), not a monkeypatched transport. A patched transport proves
the code calls a function; only a socket proves it speaks HTTP, streams
incrementally and reads a chunked body the way OpenRouter actually sends
one.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from apps.engine_core.assistant.api import (
    DEFAULT_MODEL,
    KEY_MISSING_CODE,
    MODEL_ENV,
    OPENROUTER_BASE_ENV,
    OPENROUTER_KEY_ENV,
    SETUP_ASSISTANT_SYSTEM_PROMPT,
    UPSTREAM_ERROR_CODE,
    router,
)
from tests.waits import start_uvicorn_in_thread

API = "/api/v1/assistant"

#: What the stub upstream streams back unless a test asks for something else.
#: Split mid-word on purpose: a proxy that re-tokenises or trims would join
#: these into something different from "Open DJ is importing".
STUB_DELTAS: tuple[str, ...] = ("Open ", "DJ is ", "importing")


# ----- the real upstream ---------------------------------------------------
def _sse(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


def _stub_app(
    *,
    status: int = 200,
    body: str = "",
    deltas: tuple[str, ...] = STUB_DELTAS,
    seen: list[dict[str, Any]] | None = None,
) -> FastAPI:
    """An OpenRouter-shaped stub: same path, same SSE framing, same [DONE].

    ``seen`` collects the request bodies so a test can assert what the proxy
    forwarded (model, messages, stream flag) rather than trusting it.
    """
    app = FastAPI()

    @app.post("/chat/completions")
    async def chat_completions(request: Request) -> Response:
        if seen is not None:
            seen.append(await request.json())
        if status != 200:
            return Response(content=body, status_code=status)

        def stream() -> Iterator[bytes]:
            # A keep-alive comment first: OpenRouter really sends these, and
            # a parser that treats one as a token would corrupt every reply.
            yield b": OPENROUTER PROCESSING\n\n"
            for delta in deltas:
                yield _sse({"choices": [{"delta": {"content": delta}}]})
            yield b"data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app


@contextmanager
def _serving(app: FastAPI) -> Iterator[str]:
    """Run ``app`` on a real ephemeral port and yield its base URL.

    Port 0 rather than a number: this repo reserves ports per worktree, and a
    test that squats a fixed one collides with whatever the developer has
    running. The kernel picks, we read it back off the bound socket.
    """
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server, thread = start_uvicorn_in_thread(config, what="the stub upstream")
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15.0)


# ----- fixtures ------------------------------------------------------------
@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The router alone, with the ambient environment cleared.

    A developer Mac may well export OPENROUTER_API_KEY, which would make the
    no-key test pass or fail depending on whose shell ran it. Both knobs are
    deleted here and each test sets what it needs.
    """
    monkeypatch.delenv(OPENROUTER_KEY_ENV, raising=False)
    monkeypatch.delenv(MODEL_ENV, raising=False)
    monkeypatch.delenv(OPENROUTER_BASE_ENV, raising=False)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    with TestClient(app) as test_client:
        yield test_client


def _ask(client: TestClient, text: str = "what is happening?") -> Any:
    return client.post(f"{API}/chat", json={"messages": [
        {"role": "user", "content": text},
    ]})


# ----- status --------------------------------------------------------------
def test_status_says_unconfigured_when_no_key_is_set(client: TestClient) -> None:
    """[if] status claims configured with no key [then] the UI shows a chat box that cannot work."""
    body = client.get(f"{API}/status").json()
    assert body == {"configured": False, "model": DEFAULT_MODEL}


def test_status_says_configured_once_the_key_is_present(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] status ignores the key [then] a working assistant renders as broken."""
    monkeypatch.setenv(OPENROUTER_KEY_ENV, "sk-or-test")
    assert client.get(f"{API}/status").json()["configured"] is True


def test_status_reports_the_model_override(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the override is not reported [then] the UI names a model that is not being called."""
    monkeypatch.setenv(MODEL_ENV, "x-ai/grok-4.5")
    assert client.get(f"{API}/status").json()["model"] == "x-ai/grok-4.5"


def test_default_model_is_the_verified_openrouter_slug() -> None:
    """[if] the default drifts to a guessed id [then] every default chat 404s upstream.

    Pinned to the slug read off https://openrouter.ai/api/v1/models on
    Wed 19 Aug 2026 (name 'Google: Gemini 3.7 Flash', canonical_slug
    google/gemini-3.7-flash-20260813). Changing it is a deliberate act, not
    a typo someone lands by accident.
    """
    assert DEFAULT_MODEL == "google/gemini-3.7-flash"


# ----- refusals ------------------------------------------------------------
def test_chat_refuses_without_a_key_and_names_the_env_var(
    client: TestClient,
) -> None:
    """[if] a keyless chat is not a typed 409 [then] the UI cannot explain itself."""
    response = _ask(client)
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == KEY_MISSING_CODE
    assert OPENROUTER_KEY_ENV in detail["message"]


def test_chat_rejects_an_empty_conversation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] an empty messages list reaches OpenRouter [then] we pay for a guaranteed 400."""
    monkeypatch.setenv(OPENROUTER_KEY_ENV, "sk-or-test")
    assert client.post(f"{API}/chat", json={"messages": []}).status_code == 422


# ----- the proxy, against a real server ------------------------------------
def test_chat_streams_exactly_what_upstream_sent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] deltas are dropped, reordered or re-joined [then] the reply is not the model's."""
    seen: list[dict[str, Any]] = []
    with _serving(_stub_app(seen=seen)) as base:
        monkeypatch.setenv(OPENROUTER_KEY_ENV, "sk-or-test")
        monkeypatch.setenv(OPENROUTER_BASE_ENV, base)
        with client.stream(
            "POST",
            f"{API}/chat",
            json={"messages": [{"role": "user", "content": "hi"}]},
        ) as response:
            assert response.status_code == 200
            received = "".join(response.iter_text())

    assert received == "".join(STUB_DELTAS)
    assert len(seen) == 1
    assert seen[0]["model"] == DEFAULT_MODEL
    assert seen[0]["stream"] is True


def test_chat_prepends_the_system_prompt_once(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the prompt is only client-side [then] curl and the UI get different assistants.

    Agent-native parity: the system prompt is part of the endpoint, not part
    of the browser, so an agent driving /chat with curl gets the same setup
    assistant the sidebar does.
    """
    seen: list[dict[str, Any]] = []
    with _serving(_stub_app(seen=seen)) as base:
        monkeypatch.setenv(OPENROUTER_KEY_ENV, "sk-or-test")
        monkeypatch.setenv(OPENROUTER_BASE_ENV, base)
        with client.stream(
            "POST",
            f"{API}/chat",
            json={"messages": [{"role": "user", "content": "hi"}]},
        ) as response:
            response.read()

    sent = seen[0]["messages"]
    assert sent[0] == {
        "role": "system",
        "content": SETUP_ASSISTANT_SYSTEM_PROMPT,
    }
    assert [m["role"] for m in sent] == ["system", "user"]


def test_caller_supplied_system_prompt_is_not_duplicated(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a caller's own system message is shadowed [then] the endpoint is not scriptable."""
    seen: list[dict[str, Any]] = []
    with _serving(_stub_app(seen=seen)) as base:
        monkeypatch.setenv(OPENROUTER_KEY_ENV, "sk-or-test")
        monkeypatch.setenv(OPENROUTER_BASE_ENV, base)
        with client.stream(
            "POST",
            f"{API}/chat",
            json={"messages": [
                {"role": "system", "content": "you are a beat matcher"},
                {"role": "user", "content": "hi"},
            ]},
        ) as response:
            response.read()

    assert [m["content"] for m in seen[0]["messages"]] == [
        "you are a beat matcher",
        "hi",
    ]


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (401, '{"error":{"message":"No auth credentials found"}}'),
        (402, '{"error":{"message":"Insufficient credits"}}'),
        (429, '{"error":{"message":"Rate limited"}}'),
        (500, "upstream exploded"),
        (503, "upstream unavailable"),
    ],
)
def test_upstream_failures_are_surfaced_not_swallowed(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    body: str,
) -> None:
    """[if] an upstream 4xx/5xx becomes an empty 200 [then] failure looks like silence."""
    with _serving(_stub_app(status=status, body=body)) as base:
        monkeypatch.setenv(OPENROUTER_KEY_ENV, "sk-or-test")
        monkeypatch.setenv(OPENROUTER_BASE_ENV, base)
        response = _ask(client)

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["code"] == UPSTREAM_ERROR_CODE
    assert detail["upstream_status"] == status
    # The upstream's own words survive the hop; a generic "assistant
    # unavailable" would hide 'Insufficient credits' from the one person
    # who can act on it.
    assert body[:40] in detail["message"]


def test_unreachable_upstream_fails_loudly(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a dead upstream yields an empty 200 [then] the UI invents a silent model."""
    with _serving(_stub_app()) as base:
        pass  # the server is shut down on exit; the port is now closed.
    monkeypatch.setenv(OPENROUTER_KEY_ENV, "sk-or-test")
    monkeypatch.setenv(OPENROUTER_BASE_ENV, base)
    response = _ask(client)
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == UPSTREAM_ERROR_CODE


# ----- the live smoke ------------------------------------------------------
@pytest.mark.live_openrouter
@pytest.mark.skipif(
    not os.environ.get(OPENROUTER_KEY_ENV),
    reason=f"{OPENROUTER_KEY_ENV} not set; run under `doppler run` to smoke the real model",
)
def test_live_openrouter_answers_on_the_default_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] the default model id is wrong [then] this is the only test that notices.

    Deliberately NOT part of the default suite: it spends money and needs
    network. It exists because every other test in this file would pass just
    as happily against a model slug that OpenRouter has never heard of.
    """
    monkeypatch.delenv(MODEL_ENV, raising=False)
    monkeypatch.delenv(OPENROUTER_BASE_ENV, raising=False)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    with TestClient(app) as live, live.stream(
        "POST",
        f"{API}/chat",
        json={"messages": [{
            "role": "user",
            "content": "Reply with exactly: OPEN DJ ASSISTANT ONLINE",
        }]},
    ) as response:
        assert response.status_code == 200, response.read()
        reply = "".join(response.iter_text())
    print(f"\n[live reply] {reply!r}")
    assert reply.strip(), "the default model streamed nothing back"
