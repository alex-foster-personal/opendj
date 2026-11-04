"""Wire and plug-in contract for the tool-using assistant ((private path)).

Three parties build against this file in parallel and none of them may
change it without the integrator:

- the agent loop (``agent.py``), which streams :data:`AgentEvent` lines;
- the sidebar (``agent-contract.ts`` mirrors every name here);
- tool providers (``core_tools.py``, ``machine_tools.py``), which export
  :class:`ToolSpec` tuples; and the tier chooser (``tiers.py``), which
  implements :data:`ChooseTier`.

The endpoint is stateless exactly like ``/chat``: the client resends the
whole history every turn, appending each ``message`` event verbatim, so the
engine stores no conversation.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from apps.engine_core.assistant.tiers import Intent

# ----- wire constants -------------------------------------------------------
AGENT_ROUTE: str = "/agent"
AGENT_MEDIA_TYPE: str = "application/x-ndjson"

#: Model round trips per request. A request that would need more ends with
#: ``done turn_cap`` rather than looping on the user's credit.
MAX_TOOL_ROUNDS: int = 8

#: The tool result an unapproved held call receives on the next request.
DECLINED_CODE: str = "declined_by_user"

#: Context chips go into the system prompt, so they are bounded: at most
#: this many per request, each id and label at most this many characters.
MAX_CONTEXT_REFS: int = 10
MAX_CONTEXT_CHARS: int = 200

Gate = Literal["read", "confirm"]
Tier = Literal["instant", "fast", "think"]
DoneReason = Literal["stop", "needs_confirmation", "turn_cap"]


# ----- request --------------------------------------------------------------
class ToolCall(BaseModel):
    """One call the model asked for. ``id`` is the provider's call id."""

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class AgentMessage(BaseModel):
    """One history turn, OpenAI-shaped minus the ``system`` role.

    ``system`` is absent on purpose: the engine owns the system prompt so
    curl and the sidebar get the same assistant.
    """

    role: Literal["user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None


ContextKind = Literal["deck", "playlist", "usb", "track"]


class ContextRef(BaseModel):
    """Something the user pointed the assistant at (a chip in the composer)."""

    kind: ContextKind
    id: str = Field(min_length=1, max_length=MAX_CONTEXT_CHARS)
    label: str = Field(min_length=1, max_length=MAX_CONTEXT_CHARS)


class AgentIn(BaseModel):
    messages: list[AgentMessage] = Field(min_length=1)
    #: Ids of held (``confirm``) calls in the last assistant turn that the
    #: user approved. Every other held call is answered ``declined_by_user``.
    approved_tool_call_ids: list[str] = Field(default_factory=list)
    #: The user's tier pick; None is auto. ``instant`` cannot be forced and
    #: is treated as auto.
    tier: Tier | None = None
    #: Context chips; the engine names them in the system prompt.
    context: list[ContextRef] = Field(default_factory=list, max_length=MAX_CONTEXT_REFS)


# ----- events (one JSON object per NDJSON line) ----------------------------
class ToolError(BaseModel):
    code: str
    message: str


class TierEvent(BaseModel):
    type: Literal["tier"] = "tier"
    tier: Tier
    #: None only for ``instant``, which answers without a model.
    model: str | None
    reason: str


class TextEvent(BaseModel):
    type: Literal["text"] = "text"
    delta: str


class ToolCallEvent(BaseModel):
    type: Literal["tool_call"] = "tool_call"
    call: ToolCall
    gate: Gate


class ConfirmEvent(BaseModel):
    type: Literal["confirm"] = "confirm"
    call: ToolCall
    reason: str


class ToolResultEvent(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    call_id: str
    ok: bool
    result: Any = None
    error: ToolError | None = None


class MessageEvent(BaseModel):
    type: Literal["message"] = "message"
    message: AgentMessage


class DoneEvent(BaseModel):
    type: Literal["done"] = "done"
    reason: DoneReason


class ErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    code: str
    message: str


AgentEvent = Annotated[
    TierEvent | TextEvent | ToolCallEvent | ConfirmEvent | ToolResultEvent | MessageEvent | DoneEvent | ErrorEvent,
    Field(discriminator="type"),
]


def encode_event(event: BaseModel) -> bytes:
    """One NDJSON line. The only serializer the loop may use."""
    return (event.model_dump_json() + "\n").encode("utf-8")


# ----- tool plug-in contract -----------------------------------------------
@dataclass(frozen=True)
class ToolContext:
    """What a tool may know about the request it runs in."""

    #: The engine's own origin, e.g. ``http://127.0.0.1:8682``. Tools reach
    #: the engine through its public ``/api/v1`` routes, never its internals,
    #: so the assistant can do nothing an HTTP client could not.
    base_url: str
    #: True while any deck is audible. ``confirm``-gated tools are always
    #: held; tools that are ``read`` normally may still choose to hold.
    audible: bool


class ToolFailure(Exception):
    """A tool refused or failed; becomes ``tool_result ok=false``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


ToolRun = Callable[[ToolContext, dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class ToolSpec:
    """One tool the model may call.

    ``parameters`` is a JSON Schema object sent verbatim as the function's
    parameters. ``gate="confirm"`` means the loop never runs the call until
    the user approves it in a later request.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    gate: Gate
    run: ToolRun


# ----- tier chooser contract -----------------------------------------------
@dataclass(frozen=True)
class TierChoice:
    tier: Tier
    #: OpenRouter slug; None exactly when ``tier == "instant"``.
    model: str | None
    reason: str
    #: Set exactly when ``tier == "instant"``: the rule-derived call, fresh
    #: or resumed (``tiers.Intent``).
    intent: Intent | None = None


ChooseTier = Callable[[list[AgentMessage]], TierChoice]
