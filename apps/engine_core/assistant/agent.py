"""The tool-using assistant loop behind ``POST /api/v1/assistant/agent``.

Stateless like ``/chat``: each request carries the whole history, and the
loop streams :data:`~apps.engine_core.assistant.agent_contract.AgentEvent`
lines until the model stops, a ``confirm`` call is held for the user, or
``MAX_TOOL_ROUNDS`` model round trips are spent.

One request, in order:

1. ``tier`` -- which lane answers (the injected ``choose_tier``).
2. Held calls from the previous response (calls with no tool result yet)
   are answered: approved calls of the final assistant turn run, every other
   held call gets ``declined_by_user``.
3. ``instant``: run the chooser's intent without a model, then ``done``.
   Otherwise up to ``MAX_TOOL_ROUNDS`` OpenAI-compatible completions with
   tools; ``read`` calls run at once, ``confirm`` calls end the stream with
   ``done needs_confirmation``.

Every assistant turn, with or without tool calls, and every tool result is
emitted as a ``message`` event, verbatim, so the client's next request carries
the same history the model saw. An upstream failure once the stream is open is a terminal
``error`` event: no ``done``, and never any text after it.
"""

from __future__ import annotations

import json
import unicodedata
from collections import Counter
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import BaseModel

from apps.engine_core.assistant.agent_contract import (
    DECLINED_CODE,
    MAX_TOOL_ROUNDS,
    AgentIn,
    AgentMessage,
    ConfirmEvent,
    ContextRef,
    DoneEvent,
    ErrorEvent,
    MessageEvent,
    TextEvent,
    TierChoice,
    TierEvent,
    ToolCall,
    ToolCallEvent,
    ToolContext,
    ToolError,
    ToolFailure,
    ToolResultEvent,
    ToolSpec,
    encode_event,
)
from apps.engine_core.assistant.upstream import Upstream

# ----- configuration ------------------------------------------------------
UPSTREAM_ERROR_CODE: str = "assistant_upstream_error"

#: No retries, as for /chat: a retried completion is a second charge.
_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)

AGENT_SYSTEM_PROMPT: str = (
    "You are the Open DJ assistant, inside a local-first DJ app. You act only "
    "through the tools you are given, which call this engine's own HTTP API: "
    "read status, the open performance page and the library freely; library "
    "writes and performance commands are shown to the user, who approves or "
    "declines each one. Be brief. Never say something happened unless a tool "
    "result says it did, and say plainly when a tool failed or was declined."
)


class UpstreamError(RuntimeError):
    """The model upstream refused, broke off, or sent something unparseable."""


# ----- upstream wire ------------------------------------------------------
async def sse_payloads(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    """Each JSON ``data:`` payload of an OpenAI-compatible SSE stream, to ``[DONE]``.

    Raises on a non-JSON or non-object payload, an in-band ``error``, or an end
    of stream before ``[DONE]`` rather than skipping it: a reply with a silent
    hole in it is worse than no reply.
    """
    async for line in response.aiter_lines():
        if not line.startswith("data:"):
            continue  # blank separators, ': keep-alive' comments, event:/id:
        data = line[len("data:") :].strip()
        if data == "[DONE]":
            return
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            raise UpstreamError(f"upstream sent a non-JSON SSE payload: {data[:200]!r}") from exc
        if not isinstance(payload, dict):
            raise UpstreamError(f"upstream sent a non-object SSE payload: {data[:200]!r}")
        if payload.get("error") is not None:
            raise UpstreamError(f"upstream reported an error mid-stream: {payload['error']}")
        yield payload
    raise UpstreamError("upstream closed the stream before [DONE]; the reply may be cut short")


def _wire(message: AgentMessage) -> dict[str, Any]:
    wire: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_calls:
        wire["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
            }
            for call in message.tool_calls
        ]
    if message.tool_call_id is not None:
        wire["tool_call_id"] = message.tool_call_id
    return wire


def _one_line(text: str) -> str:
    """``text`` with every whitespace and control character (any line break) as a space."""
    return "".join(" " if c.isspace() or unicodedata.category(c)[0] == "C" else c for c in text)


def system_prompt(context: Sequence[ContextRef]) -> str:
    """The agent prompt, plus the things the user pointed at, if any.

    Each chip renders on one line, so a label cannot open a prompt section.
    """
    if not context:
        return AGENT_SYSTEM_PROMPT
    refs = "\n".join(f"- {ref.kind} {_one_line(ref.id)}: {_one_line(ref.label)}" for ref in context)
    return f"{AGENT_SYSTEM_PROMPT}\n\nThe user attached these to the conversation (kind id: label):\n{refs}"


def _wire_history(history: Sequence[AgentMessage], system: str = AGENT_SYSTEM_PROMPT) -> list[dict[str, Any]]:
    """System prompt, then history with each tool result right after its call.

    OpenAI-compatible APIs reject a tool result that does not directly follow
    the assistant turn that asked for it; a client that appended a held
    call's result after a newer user message would otherwise 400 upstream.
    """
    results = {m.tool_call_id: m for m in history if m.role == "tool"}
    wire = [{"role": "system", "content": system}]
    placed: set[str] = set()
    for message in history:
        if message.role == "tool":
            continue
        wire.append(_wire(message))
        for call in message.tool_calls or []:
            if call.id in results and call.id not in placed:
                placed.add(call.id)
                wire.append(_wire(results[call.id]))
    return wire


def _tool_schema(spec: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": spec.name, "description": spec.description, "parameters": spec.parameters},
    }


@dataclass
class Turn:
    """One streamed assistant turn: its text and its (index-keyed) call fragments."""

    text: str = ""
    parts: dict[int, dict[str, str]] = field(default_factory=dict)

    def add(self, fragment: dict[str, Any]) -> None:
        index, call_id = fragment.get("index") or 0, fragment.get("id") or ""
        if not isinstance(index, int) or not isinstance(call_id, str):
            raise TypeError(f"tool call fragment has a bad index or id: {fragment}")
        part = self.parts.setdefault(index, {"id": "", "name": "", "args": ""})
        function = fragment.get("function") or {}
        part["id"] = call_id or part["id"]
        part["name"] += function.get("name") or ""
        part["args"] += function.get("arguments") or ""

    def calls(self) -> tuple[list[ToolCall], dict[str, str]]:
        """The calls, plus ``{call_id: problem}`` for arguments that are not a JSON object."""
        calls, bad = [], {}
        for part in (self.parts[i] for i in sorted(self.parts)):
            if not part["id"] or not part["name"]:
                raise UpstreamError(f"upstream sent a tool call without an id or name: {part}")
            try:
                arguments = json.loads(part["args"] or "{}")
            except json.JSONDecodeError:
                arguments = None
            if not isinstance(arguments, dict):
                bad[part["id"]] = f"arguments are not a JSON object: {part['args'][:200]!r}"
                arguments = {}
            calls.append(ToolCall(id=part["id"], name=part["name"], arguments=arguments))
        return calls, bad


def _is_choiceless_frame(payload: dict[str, Any]) -> bool:
    """A frame may omit choices only to carry usage, or (Azure) prompt-filter results."""
    return isinstance(payload.get("usage"), dict) or isinstance(payload.get("prompt_filter_results"), list)


def _take_frame(payload: dict[str, Any], turn: Turn) -> str:
    """Add one frame to ``turn`` and return its text; a frame of any other shape is an UpstreamError."""
    if isinstance(payload, dict) and payload.get("choices") in (None, []):
        if _is_choiceless_frame(payload):
            return ""
        raise UpstreamError(f"upstream sent a frame with no choices that is not a usage frame: {str(payload)[:200]!r}")
    try:
        delta = payload["choices"][0].get("delta")
        if delta is None:
            return ""
        for fragment in delta.get("tool_calls") or []:
            turn.add(fragment)
        content = delta.get("content") or ""
        turn.text += content
    except (AttributeError, IndexError, KeyError, TypeError) as exc:
        raise UpstreamError(f"upstream sent a malformed completion frame: {str(payload)[:200]!r}") from exc
    return content


async def complete(
    upstream: Upstream,
    model: str,
    messages: list[dict[str, Any]],
    tools: Sequence[ToolSpec],
    turn: Turn,
) -> AsyncGenerator[str, None]:
    """Stream one completion of wire ``messages`` into ``turn``, yielding its text deltas.

    Every failure, before or after the first byte, is an UpstreamError.
    """
    body: dict[str, Any] = {"model": model, "messages": messages, "stream": True}
    if tools:
        body["tools"] = [_tool_schema(spec) for spec in tools]
    url = f"{upstream.base_url}/chat/completions"
    try:
        async with (
            httpx.AsyncClient(timeout=_TIMEOUT) as client,
            client.stream("POST", url, json=body, headers=upstream.headers()) as response,
        ):
            if response.status_code != 200:
                text = (await response.aread()).decode("utf-8", errors="replace")
                raise UpstreamError(
                    f"{upstream.mode} upstream returned {response.status_code} for model {model}: {text[:600]}"
                )
            async for payload in sse_payloads(response):
                if content := _take_frame(payload, turn):
                    yield content
    except httpx.HTTPError as exc:
        raise UpstreamError(f"could not reach the {upstream.mode} upstream at {url}: {exc!r}") from exc


# ----- tool calls ---------------------------------------------------------
def held_calls(payload: AgentIn) -> tuple[list[ToolCall], set[str]]:
    """Calls with no tool result yet, and the ids of those that may run.

    Raises ValueError on a reused call id, a tool result that answers no
    call, or an approval for a call that is not held: each means the
    client's history is not the one this endpoint streamed.

    Only the FINAL assistant turn (followed by nothing but tool results) can
    run an approved call, on every lane. A call left unanswered in an earlier
    turn is answered ``declined_by_user`` whatever the approvals say: that is
    what the stream already did when the user moved on, and a forged earlier
    turn must not ride on a genuine last turn. The keyless instant lane
    additionally needs ``tiers.Intent.matches`` to re-derive that final
    turn's call (id, name, arguments) from the user turn before it; anything
    else needs a key.
    """
    calls = [c for m in payload.messages if m.role == "assistant" for c in m.tool_calls or []]
    if reused := sorted(i for i, n in Counter(c.id for c in calls).items() if n > 1):
        raise ValueError(f"assistant turns reuse tool call ids: {reused}")
    known = {c.id for c in calls}
    answered = {m.tool_call_id for m in payload.messages if m.role == "tool"}
    if orphans := answered - known:
        raise ValueError(f"tool results answer no tool call: {sorted(map(str, orphans))}")
    held = [c for c in calls if c.id not in answered]
    approved = set(payload.approved_tool_call_ids)
    if stray := approved - {c.id for c in held}:
        raise ValueError(f"approved ids are not held tool calls: {sorted(stray)}")
    final = _final_turn_calls(payload.messages)
    return held, {c.id for c in held if c.id in approved and c in final}


def _final_turn_calls(messages: Sequence[AgentMessage]) -> list[ToolCall]:
    """The calls of the last assistant turn, if only tool results follow it."""
    last = next((m for m in reversed(messages) if m.role != "tool"), None)
    return (last.tool_calls or []) if last is not None and last.role == "assistant" else []


def _failed(call: ToolCall, code: str, message: str) -> ToolResultEvent:
    return ToolResultEvent(call_id=call.id, ok=False, error=ToolError(code=code, message=message))


async def _execute(call: ToolCall, spec: ToolSpec | None, ctx: ToolContext) -> ToolResultEvent:
    if spec is None:
        return _failed(call, "unknown_tool", f"no tool named {call.name!r}")
    try:
        return ToolResultEvent(call_id=call.id, ok=True, result=await spec.run(ctx, call.arguments))
    except ToolFailure as exc:
        return _failed(call, exc.code, exc.message)
    except Exception as exc:  # noqa: BLE001  # a tool bug must still end the stream with a result
        return _failed(call, "tool_error", f"{type(exc).__name__}: {exc}")


def _confirm_reason(audible: bool) -> str:
    """The card already names the tool by its label, so this says only what waits and why."""
    reason = "Runs only after your approval"
    return reason + (" - a deck is audible, so the room will hear it" if audible else "")


@dataclass
class _Loop:
    """One request's working state: the history the model sees and the tool table."""

    history: list[AgentMessage]
    tools: dict[str, ToolSpec]
    ctx: ToolContext

    def answer(self, result: ToolResultEvent) -> list[BaseModel]:
        """The result event and the tool message that carries it in history."""
        content = result.result if result.ok else {"error": result.error.model_dump() if result.error else None}
        message = AgentMessage(role="tool", tool_call_id=result.call_id, content=json.dumps(content))
        self.history.append(message)
        return [result, MessageEvent(message=message)]

    async def resolve_held(self, held: list[ToolCall], approved: set[str]) -> AsyncIterator[BaseModel]:
        for call in held:
            if call.id in approved:
                result = await _execute(call, self.tools.get(call.name), self.ctx)
            else:
                result = _failed(call, DECLINED_CODE, "the user declined this call")
            for event in self.answer(result):
                yield event

    async def take_turn(
        self, text: str, calls: list[ToolCall], bad: dict[str, str], held: list[ToolCall]
    ) -> AsyncIterator[BaseModel]:
        """Record the assistant turn, run its read calls, hold its confirm calls."""
        turn = AgentMessage(role="assistant", content=text or None, tool_calls=calls or None)
        self.history.append(turn)
        yield MessageEvent(message=turn)
        for call in calls:
            spec = self.tools.get(call.name)
            gate = spec.gate if spec is not None else "read"
            yield ToolCallEvent(call=call, gate=gate)
            if call.id in bad:
                results = self.answer(_failed(call, "invalid_arguments", bad[call.id]))
            elif gate == "confirm":
                held.append(call)
                results = [ConfirmEvent(call=call, reason=_confirm_reason(self.ctx.audible))]
            else:
                results = self.answer(await _execute(call, spec, self.ctx))
            for event in results:
                yield event


# ----- the loop -----------------------------------------------------------
async def _events(
    payload: AgentIn,
    held_now: tuple[list[ToolCall], set[str]],
    choice: TierChoice,
    upstream: Upstream | None,
    tools: Sequence[ToolSpec],
    ctx: ToolContext,
) -> AsyncIterator[BaseModel]:
    loop = _Loop(history=list(payload.messages), tools={t.name: t for t in tools}, ctx=ctx)
    yield TierEvent(tier=choice.tier, model=choice.model, reason=choice.reason)
    async for event in loop.resolve_held(*held_now):
        yield event

    held: list[ToolCall] = []
    if choice.tier == "instant":
        if choice.intent is not None and not choice.intent.resumed:
            async for event in loop.take_turn("", [choice.intent.call], {}, held):
                yield event
        yield DoneEvent(reason="needs_confirmation" if held else "stop")
        return

    if upstream is None or choice.model is None:
        raise ValueError(f"tier {choice.tier} needs an upstream and a model")
    async for event in _model_rounds(loop, upstream, choice.model, system_prompt(payload.context), tools):
        yield event


async def _model_rounds(
    loop: _Loop, upstream: Upstream, model: str, system: str, tools: Sequence[ToolSpec]
) -> AsyncIterator[BaseModel]:
    held: list[ToolCall] = []
    for _ in range(MAX_TOOL_ROUNDS):
        turn = Turn()
        try:
            async for delta in complete(upstream, model, _wire_history(loop.history, system), tools, turn):
                yield TextEvent(delta=delta)
            calls, bad = turn.calls()
        except UpstreamError as exc:
            yield ErrorEvent(code=UPSTREAM_ERROR_CODE, message=str(exc))
            return
        if calls or turn.text:
            async for event in loop.take_turn(turn.text, calls, bad, held):
                yield event
        if held:
            yield DoneEvent(reason="needs_confirmation")
            return
        if not calls:
            yield DoneEvent(reason="stop")
            return
    yield DoneEvent(reason="turn_cap")


async def run_agent(
    payload: AgentIn,
    *,
    held: tuple[list[ToolCall], set[str]],
    choice: TierChoice,
    upstream: Upstream | None,
    tools: Sequence[ToolSpec],
    ctx: ToolContext,
) -> AsyncIterator[bytes]:
    """The NDJSON body of one ``/agent`` response.

    ``held`` is ``held_calls(payload)`` as returned, already validated by the
    caller, so only its runnable ids execute;
    ``upstream`` is None only for instant.
    """
    async for event in _events(payload, held, choice, upstream, tools, ctx):
        yield encode_event(event)


__all__ = [
    "AGENT_SYSTEM_PROMPT",
    "UPSTREAM_ERROR_CODE",
    "Turn",
    "UpstreamError",
    "complete",
    "held_calls",
    "run_agent",
    "sse_payloads",
    "system_prompt",
]
