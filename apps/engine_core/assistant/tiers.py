"""Per-turn tier chooser for the agent loop ((private path), P3).

Rule-first, no network. :func:`choose_tier` implements
:data:`~apps.engine_core.assistant.agent_contract.ChooseTier`:

- ``instant``: the last message is a user turn that parses as one
  unambiguous transport or machine command, or answers the call such a
  turn derived. No model; the returned :class:`Intent` owns the call.
- ``think``: a multi-step planning request, or a long conversation.
- ``fast``: everything else.

Models are read from the environment at call time. The optional router
(``MDT_ASSISTANT_ROUTER=1``) is an OpenRouter slug that picks its own model
per turn; enabling it only swaps the think lane's slug, so this module never
makes the routing call itself. It is off by default because a router call is
not fast (research note section 5).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from apps.engine_core.assistant.agent_contract import AgentMessage, ChooseTier, Tier, TierChoice, ToolCall
from apps.voice.grammar import NUMBER_WORDS

# ----- configuration ------------------------------------------------------
#: Which model to call. Overridable because the maintainer named two candidates
#: (an OpenRouter-hosted Terra agent and Gemini Flash 3.7) and OpenRouter
#: serves both behind one API -- switching is an env var, not a code change.
MODEL_ENV: str = "MDT_ASSISTANT_MODEL"

#: Gemini Flash 3.7's OpenRouter slug, read off
#: https://openrouter.ai/api/v1/models on Wed 19 Aug 2026 -- listed there as
#: "Google: Gemini 3.7 Flash", canonical_slug google/gemini-3.7-flash-20260813,
#: 1,048,576-token context. NOT guessed from the product name: OpenRouter's
#: slugs invert Google's marketing order (gemini-3.7-flash, not
#: gemini-flash-3.7) and a wrong slug is a 404 on every single request.
DEFAULT_MODEL: str = "google/gemini-3.7-flash"

FAST_MODEL_ENV: str = "MDT_ASSISTANT_FAST_MODEL"
THINK_MODEL_ENV: str = "MDT_ASSISTANT_THINK_MODEL"
ROUTER_FLAG_ENV: str = "MDT_ASSISTANT_ROUTER"
ROUTER_MODEL_ENV: str = "MDT_ASSISTANT_ROUTER_MODEL"

#: User turns at which a conversation goes to the think lane regardless of
#: what the last message asks: by then the fast model is juggling context.
LONG_CONVERSATION_USER_TURNS: int = 12

#: The real tools instant intents call (core_tools.py, machine_tools.py).
#: Deck and mixer intents go through ``command`` in CLI verb grammar, so
#: they pass the same parser and confirm gate a model's call would.
COMMAND_TOOL: str = "command"
SYSTEM_VOLUME_TOOL: str = "system_volume_set"

#: Instant call ids are this prefix plus the history position.
INSTANT_ID_PREFIX: str = "instant_"

#: Valid integer slot values. A parse outside these is not unambiguous, so it
#: falls through to a model instead of being clamped.
SLOT_RANGES: dict[str, range] = {"deck": range(1, 5), "percent": range(101)}


class RouterUnconfigured(RuntimeError):
    """``MDT_ASSISTANT_ROUTER`` is on but names no router model."""


@dataclass(frozen=True)
class TierModels:
    fast: str
    think: str
    #: True when ``think`` is the router slug rather than a named model.
    routed: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> TierModels:
        base = _env(env, MODEL_ENV) or DEFAULT_MODEL
        fast = _env(env, FAST_MODEL_ENV) or base
        if _env(env, ROUTER_FLAG_ENV).lower() not in ("1", "true", "yes", "on"):
            return cls(fast=fast, think=_env(env, THINK_MODEL_ENV) or base)
        router = _env(env, ROUTER_MODEL_ENV)
        if not router:
            raise RouterUnconfigured(f"{ROUTER_FLAG_ENV} is on but {ROUTER_MODEL_ENV} is empty")
        return cls(fast=fast, think=router, routed=True)


def _env(env: Mapping[str, str], name: str) -> str:
    return env.get(name, "").strip()


# ----- rules --------------------------------------------------------------
_PCT = r" ?(?:%|percent)?"
_TO = r"(?:to |at )?"


Slots = dict[str, int]


@dataclass(frozen=True)
class _Rule:
    tier: Tier
    pattern: re.Pattern[str]
    reason: str
    #: Instant only: the tool to call, the integer slots it requires, and
    #: the tool arguments built from them.
    tool: str = ""
    slots: tuple[str, ...] = ()
    arguments: Callable[[Slots], dict[str, Any]] | None = None


#: First match wins. Instant entries are anchored commands; think entries
#: search anywhere in the message. A command with no exact real tool (USB
#: eject needs a volume id, there is no load-next verb) is deliberately
#: absent, so it reaches a model that can ask or look it up.
_RULES: tuple[_Rule, ...] = (
    _Rule(
        "instant",
        re.compile(r"^(?:play|start|resume) (?:on )?deck (?P<deck>\d+)$"),
        "rule: play command",
        COMMAND_TOOL,
        ("deck",),
        lambda s: {"verb": f"play {s['deck']}"},
    ),
    _Rule(
        "instant",
        re.compile(r"^(?:pause|stop) deck (?P<deck>\d+)$"),
        "rule: pause command",
        COMMAND_TOOL,
        ("deck",),
        lambda s: {"verb": f"pause {s['deck']}"},
    ),
    _Rule(
        "instant",
        re.compile(rf"^(?:set |turn )?(?:the )?master(?: volume| level)? {_TO}(?P<percent>\d+){_PCT}$"),
        "rule: master volume command",
        COMMAND_TOOL,
        ("percent",),
        lambda s: {"verb": f"master_volume {s['percent'] / 100:g}"},
    ),
    _Rule(
        "instant",
        re.compile(rf"^(?:set |turn )?(?:the )?(?:system|computer|mac|laptop) volume {_TO}(?P<percent>\d+){_PCT}$"),
        "rule: system volume command",
        SYSTEM_VOLUME_TOOL,
        ("percent",),
        lambda s: {"volume": s["percent"]},
    ),
    _Rule(
        "think",
        re.compile(
            r"\b(?:build|plan|make|create|put together|design|prepare)\b.*\b(?:a|an|my|the)\b.*\bset\b"
            r"|\bset ?list\b"
        ),
        "planning request: build a set",
    ),
    _Rule(
        "think",
        re.compile(r"\btransitions?\b.*\b(?:chain|sequence|order)\b|\bplan\b.*\btransitions?\b"),
        "planning request: transition chain",
    ),
    _Rule(
        "think",
        re.compile(r"\b(?:reorgani[sz]e|restructure|reorder|regroup|clean up|tidy)\b.*\bplaylists?\b"),
        "planning request: reorganise playlists",
    ),
    _Rule("think", re.compile(r"\bstep by step\b|\bmulti[- ]step\b"), "planning request: multi-step"),
)

_NUMBER_WORD = re.compile(r"\b(" + "|".join(NUMBER_WORDS) + r")\b")
_POLITE_PREFIX = re.compile(r"^(?:(?:please|can you|could you|hey) )+")


def _normalise(text: str) -> str:
    t = re.sub(r"\s+", " ", text.strip().lower())
    t = re.sub(r"[.!]+$", "", t)
    t = re.sub(r",? please$", "", t)
    t = _POLITE_PREFIX.sub("", t)
    return _NUMBER_WORD.sub(lambda m: str(NUMBER_WORDS[m.group(1)]), t)


def _first_match(text: str, instant_ok: bool) -> tuple[_Rule, Slots] | None:
    """The first rule matching ``text`` with every slot in range, and its slots."""
    normalised = _normalise(text)
    for rule in _RULES:
        match = rule.pattern.search(normalised) if instant_ok or rule.tier != "instant" else None
        slots = {name: int(match[name]) for name in rule.slots} if match else {}
        if match and all(value in SLOT_RANGES[name] for name, value in slots.items()):
            return rule, slots
    return None


# ----- chooser ------------------------------------------------------------
def _last_user_text(messages: list[AgentMessage]) -> str:
    return next((m.content or "" for m in reversed(messages) if m.role == "user"), "")


@dataclass(frozen=True)
class Intent:
    """The instant lane: one rule-derived call that needs no model and no key.

    Every instant choice carries one. ``resumed`` is False for a fresh
    intent (the loop proposes ``call``) and True when the request only
    answers that call, held from the previous response (the loop runs or
    declines it and stops). Either way no model is involved, so the route
    needs no upstream and a picker override never moves it.
    """

    reason: str
    call: ToolCall
    resumed: bool = False

    @staticmethod
    def call_id(position: int) -> str:
        """``instant_<history position>``: unique per conversation, never reused."""
        return f"{INSTANT_ID_PREFIX}{position}"

    @classmethod
    def derive(cls, messages: list[AgentMessage]) -> Intent | None:
        """What the instant rules derive when ``messages`` ends with a user turn."""
        if not messages or messages[-1].role != "user":
            return None
        hit = _first_match(messages[-1].content or "", instant_ok=True)
        if hit is None:
            return None
        rule, slots = hit
        if rule.arguments is None:
            return None
        call = ToolCall(id=cls.call_id(len(messages)), name=rule.tool, arguments=rule.arguments(slots))
        return cls(reason=rule.reason, call=call)

    @classmethod
    def of_text(cls, text: str) -> Intent | None:
        """What the instant rules derive from ``text`` as a conversation's first turn."""
        return cls.derive([AgentMessage(role="user", content=text)])

    def matches(self, held_turn: AgentMessage) -> bool:
        """``held_turn`` holds exactly this call and nothing else, byte for byte.

        The history is client-supplied, so an ``instant_`` id alone proves
        nothing: ``12`` is not ``12.0`` and ``1`` is not ``true``.
        """
        return held_turn.role == "assistant" and [_canon(c) for c in held_turn.tool_calls or []] == [_canon(self.call)]


def _canon(call: ToolCall) -> str:
    return json.dumps(call.model_dump(), sort_keys=True)


def _instant_intent(messages: list[AgentMessage]) -> Intent | None:
    """A fresh intent from the last user turn, or the held one this request answers."""
    if fresh := Intent.derive(messages):
        return fresh
    held = Intent.derive(messages[:-1])
    if held is None or not held.matches(messages[-1]):
        return None
    return Intent(reason="resume: answer the held instant call", call=held.call, resumed=True)


def resumes_instant(messages: list[AgentMessage]) -> bool:
    """The request only answers the instant call the server derived last turn."""
    intent = _instant_intent(messages)
    return intent is not None and intent.resumed


def apply_override(choice: TierChoice, override: Tier | None, models: TierModels) -> TierChoice:
    """The user's picker choice over the rule's, except instant: that is never forced.

    A fresh intent is exact and free, and a resumed intent's call is already
    held: sending it to a model would re-decide something the user is answering.
    """
    if override in (None, "instant") or choice.intent is not None:
        return choice
    model = models.fast if override == "fast" else models.think
    return TierChoice(tier=override, model=model, reason=f"user override: {override}")


def choose_tier_with(messages: list[AgentMessage], models: TierModels) -> TierChoice:
    """:func:`choose_tier` against explicit models.

    Instant fires only when the last message is the user's: a request that
    resumes after a tool result or an approval must not re-run the command.
    """
    if not messages:
        return TierChoice(tier="fast", model=models.fast, reason="default: empty history")
    if intent := _instant_intent(messages):
        return TierChoice(tier="instant", model=None, reason=intent.reason, intent=intent)
    hit = _first_match(_last_user_text(messages), instant_ok=False)
    think_reason = hit[0].reason if hit else None
    user_turns = sum(m.role == "user" for m in messages)
    if think_reason is None and user_turns >= LONG_CONVERSATION_USER_TURNS:
        think_reason = f"long conversation: {user_turns} user turns"
    if think_reason is not None:
        suffix = " (router)" if models.routed else ""
        return TierChoice(tier="think", model=models.think, reason=think_reason + suffix)
    return TierChoice(tier="fast", model=models.fast, reason="default: short request")


def choose_tier(messages: list[AgentMessage]) -> TierChoice:
    """The agent loop's :data:`ChooseTier`; reads models from the env per call."""
    return choose_tier_with(messages, TierModels.from_env(os.environ))


_: ChooseTier = choose_tier
