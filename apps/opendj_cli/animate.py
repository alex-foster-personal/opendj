"""ANIM-03: ``animate_control(deck, control, expr)``, the agent twin of a dial's Animate box.

The expression is the tail of the CLI's existing ramp, not a new language::

    opendj eq 1 low 0 --over 8bars --anchor next_downbeat
    opendj animate 1 eq_low "0 --over 8bars --anchor next_downbeat"   # the same ramp

Grammar (each option at most once, ``--over`` required, any order after the target)::

    expr     := target ( --over duration | --anchor anchor | --clock clock )*
    target   := a number in 0..1 (0.5 is flat for EQ, TRIM and FILTER)
    duration := N beat(s)|bar(s)|phrase(s)|ms ; N > 0, whole unless ms
    anchor   := next_beat | next_downbeat | next_phrase
    clock    := master | 1..4

The page twin is ``apps/webui/frontend/src/lib/rb/animate-expr.ts``. Both are
held to the same accept/refuse cases and the same one-line messages by
``tests/opendj_cli/fixtures/animate_expr_vectors.json``.

Requirements (mini-PRD):
  ✔︎ ✅ A valid expression becomes the AGENT-03 ``ramp`` order the CLI's ``--over`` builds.
    [if] ``animate 1 eq_low "0 --over 8bars"`` differs from ``eq 1 low 0 --over 8bars`` [then ⛔️]
  ✔︎ ✅ A bad one is refused offline with the failing token named.
    [if] ``@8 0`` is refused without naming ``@8`` [then ⛔️]
    [if] the crossfader is given a deck, or an EQ is not [then ⛔️]
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from apps.opendj_cli.orders import ramp
from apps.opendj_cli.verbs import Invocation, InvocationError, parse_invocation

ANIMATE_COMMAND = "animate"

_TARGET_RE = re.compile(r"^(?:\d+(?:\.\d*)?|\.\d+)$")
_DURATION_RE = re.compile(r"^(\d+(?:\.\d*)?|\.\d+)(ms|beats?|bars?|phrases?)$")
_UNITS: dict[str, str] = {
    "ms": "ms",
    "beat": "beats", "beats": "beats",
    "bar": "bars", "bars": "bars",
    "phrase": "phrases", "phrases": "phrases",
}
_ANCHORS: tuple[str, ...] = ("next_beat", "next_downbeat", "next_phrase")
_OPTIONS: tuple[str, ...] = ("--over", "--anchor", "--clock")
_DURATION_HINT = "--over needs a duration like 4beats, 2bars, 1phrase or 500ms"


@dataclass(frozen=True)
class Control:
    """One animatable control, named by the CLI verb that sets it."""

    verb: str
    part: str | None = None
    per_deck: bool = True

    def tokens(self, deck: int | None, value: float) -> list[str]:
        """The verb invocation that sets this control to ``value``: ``eq 1 low 0``."""
        scope = [str(deck)] if self.per_deck else []
        part = [] if self.part is None else [self.part]
        return [self.verb, *scope, *part, repr(value)]


CONTROLS: dict[str, Control] = {
    "eq_low": Control("eq", "low"),
    "eq_mid": Control("eq", "mid"),
    "eq_high": Control("eq", "high"),
    "trim": Control("trim"),
    "filter": Control("filter"),
    "fader": Control("fader"),
    "crossfader": Control("crossfader", per_deck=False),
    "stem_vocal": Control("stem_gain", "vocal"),
    "stem_instrumental": Control("stem_gain", "instrumental"),
    "stem_drums": Control("stem_gain", "drums"),
    "stem_bass": Control("stem_gain", "bass"),
}


class AnimateExprError(InvocationError):
    """The expression does not parse; ``token`` is the one that failed, if any."""

    def __init__(self, token: str | None, message: str) -> None:
        self.token = token
        self.reason = message
        super().__init__(message if token is None else f'"{token}": {message}')


# ----- grammar ---------------------------------------------------------------

def _duration(raw: str) -> dict[str, Any]:
    match = _DURATION_RE.match(raw.lower())
    if match is None:
        raise AnimateExprError(raw, _DURATION_HINT)
    n = float(match.group(1))
    unit = _UNITS[match.group(2)]
    if not n > 0:
        raise AnimateExprError(raw, "a duration must be greater than zero")
    if unit != "ms" and not n.is_integer():
        raise AnimateExprError(raw, f"{unit} must be a whole number")
    return {"unit": unit, "n": n if unit == "ms" else int(n)}


def _options(rest: list[str]) -> dict[str, str]:
    seen: dict[str, str] = {}
    for index in range(0, len(rest), 2):
        option = rest[index]
        if option not in _OPTIONS:
            raise AnimateExprError(option, "unknown option; expected --over, --anchor or --clock")
        if option in seen:
            raise AnimateExprError(option, "given twice")
        if index + 1 >= len(rest):
            raise AnimateExprError(option, "missing its value")
        seen[option] = rest[index + 1]
    return seen


def parse_animate_expr(text: str) -> tuple[float, dict[str, Any]]:
    """``"0 --over 8bars --anchor next_downbeat"`` -> ``(0.0, Duration)``."""
    tokens = text.split()
    if not tokens:
        raise AnimateExprError(None, "empty: type a target then --over, e.g. 0 --over 8bars")
    target, *rest = tokens
    if _TARGET_RE.match(target) is None:
        raise AnimateExprError(target, "expected a target value 0..1 first, e.g. 0 --over 8bars")
    to = float(target)
    if to > 1:
        raise AnimateExprError(
            target, "target must be within 0..1 (0.5 is flat for EQ, TRIM and FILTER)"
        )
    seen = _options(rest)
    if "--over" not in seen:
        raise AnimateExprError(None, "missing --over <duration>, e.g. 0 --over 8bars")
    over = _duration(seen["--over"])
    anchor = seen.get("--anchor")
    if anchor is not None:
        if anchor not in _ANCHORS:
            raise AnimateExprError(
                anchor, "--anchor must be next_beat, next_downbeat or next_phrase"
            )
        over["anchor"] = anchor
    clock = seen.get("--clock")
    if clock is not None:
        if clock == "master":
            over["clock"] = "master"
        elif re.fullmatch(r"[1-4]", clock):
            over["clock"] = int(clock)
        else:
            raise AnimateExprError(clock, "--clock must be master or 1..4")
    return to, over


# ----- order -------------------------------------------------------------------

def _control(name: str) -> Control:
    if name not in CONTROLS:
        raise InvocationError(
            f"unknown control {name!r}; expected one of {', '.join(CONTROLS)}"
        )
    return CONTROLS[name]


def _deck(name: str, control: Control, deck: int | None) -> int | None:
    if not control.per_deck:
        if deck is not None:
            raise InvocationError(f"{name} belongs to no deck; omit deck")
        return None
    if deck not in (1, 2, 3, 4):
        raise InvocationError(f"{name} needs deck 1..4, got {deck!r}")
    return deck


def animate_invocation(
    deck: int | None, control: str, expr: str
) -> tuple[Invocation, dict[str, Any], dict[str, Any]]:
    """(the verb invocation, the ramp order, its Duration), refused offline when malformed.

    The order is built by the SAME ``ramp`` the CLI's ``--over`` uses, from the
    verb that sets the control, so ``animate 1 eq_low "0 --over 8bars"`` and
    ``eq 1 low 0 --over 8bars`` cannot drift apart.
    """
    spec = _control(control)
    deck_id = _deck(control, spec, deck)
    to, over = parse_animate_expr(expr)
    invocation = parse_invocation(spec.tokens(deck_id, to))
    return invocation, ramp(invocation, over), over


def animate_order(deck: int | None, control: str, expr: str) -> dict[str, Any]:
    """The AGENT-03 ``ramp`` order for one animation."""
    return animate_invocation(deck, control, expr)[1]


def parse_animate_tokens(tokens: list[str]) -> tuple[Invocation, dict[str, Any], dict[str, Any]]:
    """``animate [<deck>] <control> "<expr>"`` argv -> (invocation, order, over)."""
    if len(tokens) == 3:
        raw_deck, control, expr = tokens
        if not raw_deck.isdigit():
            raise InvocationError(f"deck must be 1..4, got {raw_deck!r}")
        return animate_invocation(int(raw_deck), control, expr)
    if len(tokens) == 2:
        control, expr = tokens
        return animate_invocation(None, control, expr)
    raise InvocationError(
        'usage: opendj animate [<deck>] <control> "<to> --over <duration> '
        '[--anchor A] [--clock C]" (quote the expression)'
    )
