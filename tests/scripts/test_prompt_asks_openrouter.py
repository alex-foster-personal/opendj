"""REQS-12670 OpenRouter boundary and UNAVAILABLE helper for scripts.prompt_asks (issue #12670).

[if] the model is unavailable or the adapter runs [then] UNAVAILABLE is explicit and the adapter is real, [else stop].
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from scripts import prompt_asks as asks

pytestmark = pytest.mark.requirement("REQS-12670")

_UNCLASSIFIED_PROMPT = "Improve the waveform renderer"


def test_boolean_helper_raises_when_model_unavailable() -> None:
    """[if] the LLM is unreachable [then] is_behaviour_bearing raises, never False, [else stop]."""

    def _broken(_text: str) -> asks.PromptAskClassification:
        raise asks.PromptAskModelUnavailable("network down")

    assert asks.classify(_UNCLASSIFIED_PROMPT) == asks.UNCLASSIFIED
    with pytest.raises(asks.PromptAskModelUnavailable, match="network down"):
        asks.is_behaviour_bearing(_UNCLASSIFIED_PROMPT, llm=_broken)


def test_boolean_helper_raises_on_missing_key_through_real_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] OPENROUTER_API_KEY is absent [then] the real adapter path raises naming it, [else stop]."""
    monkeypatch.delenv(asks.KEY_ENV, raising=False)
    monkeypatch.setenv(asks.MODEL_ENV, "any/model")
    with pytest.raises(asks.PromptAskModelUnavailable, match=asks.KEY_ENV):
        asks.is_behaviour_bearing(_UNCLASSIFIED_PROMPT)


class _Recorder(BaseHTTPRequestHandler):
    """Local OpenRouter stand-in: records one POST and answers with ``reply``."""

    seen: list[dict[str, Any]] = []
    reply: dict[str, Any] = {}

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        """Record the request and send the canned chat completion."""
        length = int(self.headers.get("Content-Length", "0"))
        type(self).seen.append({"auth": self.headers.get("Authorization"), "body": json.loads(self.rfile.read(length))})
        data = json.dumps(type(self).reply).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args: Any) -> None:
        """Keep pytest output quiet."""


@pytest.fixture
def local_openrouter(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[_Recorder]]:
    """Serve _Recorder on localhost and point the adapter's URL and env at it."""
    _Recorder.seen = []
    server = HTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(asks, "OPENROUTER_CHAT_URL", f"http://127.0.0.1:{server.server_port}/chat")
    monkeypatch.setenv(asks.KEY_ENV, "test-key")
    monkeypatch.setenv(asks.MODEL_ENV, "test/model")
    try:
        yield _Recorder
    finally:
        server.shutdown()
        server.server_close()


def _completion(content: str) -> dict[str, Any]:
    """Build a chat-completions payload carrying ``content``."""
    return {"choices": [{"message": {"content": content}}]}


def test_adapter_sends_strict_schema_and_validates_reply(local_openrouter: type[_Recorder]) -> None:
    """[if] the adapter runs [then] it posts the strict schema and parses the verdict, [else stop]."""
    local_openrouter.reply = _completion(json.dumps({"behaviour_bearing": True, "reason": "asks a change"}))
    verdict = asks._openrouter_classify(_UNCLASSIFIED_PROMPT)
    assert verdict == asks.PromptAskClassification(behaviour_bearing=True, reason="asks a change")
    (seen,) = local_openrouter.seen
    assert seen["auth"] == "Bearer test-key"
    body = seen["body"]
    assert body["model"] == "test/model"
    assert body["messages"][-1] == {"role": "user", "content": _UNCLASSIFIED_PROMPT}
    fmt = body["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True
    assert set(fmt["json_schema"]["schema"]["required"]) == {"behaviour_bearing", "reason"}


def test_adapter_rejects_reply_outside_schema(local_openrouter: type[_Recorder]) -> None:
    """[if] the provider reply fails the pydantic schema [then] UNAVAILABLE, not a guess, [else stop]."""
    local_openrouter.reply = _completion(json.dumps({"behaviour_bearing": "maybe"}))
    with pytest.raises(asks.PromptAskModelUnavailable, match="failed validation"):
        asks._openrouter_classify(_UNCLASSIFIED_PROMPT)


def test_real_openrouter_boundary() -> None:
    """[if] OpenRouter credentials are set [then] the real provider returns a valid verdict, [else stop]."""
    missing = [name for name in (asks.KEY_ENV, asks.MODEL_ENV) if not os.environ.get(name, "").strip()]
    if missing:
        pytest.skip(f"UNAVAILABLE: {', '.join(missing)} not set, so the real OpenRouter path is unmeasured here")
    try:
        verdict = asks._openrouter_classify("Improve the waveform renderer so peaks stay visible.")
    except asks.PromptAskModelUnavailable as exc:
        pytest.skip(f"UNAVAILABLE: OpenRouter unreachable: {exc}")
    assert isinstance(verdict, asks.PromptAskClassification)
    assert verdict.reason.strip()
