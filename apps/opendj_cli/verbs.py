"""Turn a CLI invocation into one AGENT-03 command.

``[deck <n>] <verb> [positionals...]`` is the whole grammar, and the ``do``
script uses the same one inside each quoted step, so ``opendj deck 1 play``,
``opendj play 1`` and ``do "play 1"`` all build the identical command. The
verb table itself lives in :mod:`apps.opendj_cli.catalog`.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from apps.opendj_cli.catalog import RAMPABLE_TYPES, VERBS, Verb
from apps.opendj_cli.kinds import DECK_VALUES, Arg, deck_value

DURATION_UNITS: dict[str, str] = {
    "ms": "ms",
    "beat": "beats", "beats": "beats",
    "bar": "bars", "bars": "bars",
    "phrase": "phrases", "phrases": "phrases",
}
DURATION_ANCHORS: tuple[str, ...] = ("next_beat", "next_downbeat", "next_phrase")

_DURATION_HINT = "--over needs a duration like 4beats, 2bars, 1phrase or 500ms"


class InvocationError(ValueError):
    """The tokens do not name a verb, or a value is outside its domain."""


@dataclass(frozen=True)
class Invocation:
    """One parsed verb call: the bus command plus the raw parsed values."""

    verb: Verb
    command: dict[str, Any]
    values: dict[str, Any] = field(default_factory=dict)


def _consume(args: Sequence[Arg], rest: Sequence[str]) -> tuple[dict[str, Any], list[str]]:
    values: dict[str, Any] = {}
    remaining = list(rest)
    for argument in args:
        if len(remaining) < argument.arity:
            if argument.optional:
                continue
            if argument.arity > 1:
                raise InvocationError(
                    f"<{argument.key}> needs {argument.arity} numbers (in_ms then out_ms)"
                )
            raise InvocationError(f"missing <{argument.key}> for this verb")
        token = " ".join(remaining[: argument.arity])
        try:
            values[argument.key] = argument.parse(argument.key, token)
        except ValueError as error:
            raise InvocationError(str(error)) from error
        del remaining[: argument.arity]
    return values, remaining


def _split_deck_scope(tokens: Sequence[str]) -> tuple[int | None, list[str]]:
    """Pop a leading ``deck <n>``, which is how ``opendj deck 1 play`` reads."""
    if len(tokens) >= 2 and tokens[0] == "deck":
        return deck_value("deck", tokens[1]), list(tokens[2:])
    if tokens[:1] == ["deck"]:
        raise InvocationError("deck scope needs a deck number, as in: deck 1 play")
    return None, list(tokens)


def _verb_for(name: str, scoped: int | None, positionals: list[str]) -> tuple[Verb, list[str]]:
    """Look the verb up and, given a scope prefix, put its deck back in place."""
    try:
        verb = VERBS[name]
    except KeyError:
        raise InvocationError(f"unknown verb {name!r}; run opendj --list-verbs") from None
    if scoped is None:
        return verb, positionals
    if not any(argument.key == "deck" for argument in verb.args):
        raise InvocationError(f"{name} does not take a deck")
    return verb, [str(scoped), *positionals]


def _reject_leftover(
    verb: Verb, values: Mapping[str, Any], leftover: Sequence[str], takes_deck: bool
) -> None:
    if not leftover:
        return
    if takes_deck and leftover[0].lstrip("-").isdigit():
        raise InvocationError(
            f"deck was given twice ({values.get('deck')} and {leftover[0]}); give it "
            "once, either as `deck <n> <verb>` or after the verb"
        )
    raise InvocationError(
        f"{verb.name} does not take {leftover[0]!r}; expected "
        f"{verb.usage() or 'no arguments'}"
    )


def parse_invocation(tokens: Sequence[str], deck_scope: int | None = None) -> Invocation:
    """Parse ``[deck <n>] <verb> [positionals...]`` into one bus command.

    A verb that takes a deck accepts it either as the scope prefix
    (``opendj deck 1 play``) or as its first positional (``opendj eq 2 low
    0.5``); naming both differently is refused rather than guessed.
    """
    scoped, rest = _split_deck_scope(tokens)
    if deck_scope is not None:
        if scoped is not None and scoped != deck_scope:
            raise InvocationError(
                f"deck {deck_scope} was given twice, and the second one is {scoped}"
            )
        scoped = deck_scope
    if not rest:
        raise InvocationError("no verb given; run opendj --list-verbs")
    name, *positionals = rest
    verb, positionals = _verb_for(name, scoped, positionals)
    values, leftover = _consume(verb.args, positionals)
    _reject_leftover(verb, values, leftover, any(a.key == "deck" for a in verb.args))
    command = verb.build(values)
    if "deck" in command and command["deck"] not in DECK_VALUES:
        raise InvocationError(f"deck must be one of 1, 2, 3, 4; got {command['deck']!r}")
    return Invocation(verb=verb, command=command, values=values)


def _split_duration(raw: str) -> tuple[str, str]:
    """``4bars`` -> ``("4", "bars")``, refusing anything that is not a Duration."""
    text = raw.strip().lower()
    for suffix in sorted(DURATION_UNITS, key=len, reverse=True):
        if text.endswith(suffix) and text[: -len(suffix)] != "":
            return text[: -len(suffix)], DURATION_UNITS[suffix]
    raise InvocationError(f"{_DURATION_HINT}; got {raw!r}")


def _duration_n(raw: str, magnitude: str, unit: str) -> float:
    """The magnitude, held to the bounds ``agent-duration.ts`` enforces."""
    try:
        n = float(magnitude)
    except ValueError:
        raise InvocationError(f"{_DURATION_HINT}; got {raw!r}") from None
    if not math.isfinite(n):
        # Checked BEFORE the sign test, which catches nan only by accident:
        # `not nan > 0` is True, so nan was refused for the wrong reason and
        # named the wrong problem. `inf` was not refused at all on the ms path,
        # because `is_integer()` never runs there, and an infinite duration
        # becomes an infinite request deadline: the same never-expiring wait
        # `--settle nan` produced.
        raise InvocationError(f"a duration must be a finite number, got {raw!r}")
    if not n > 0:
        raise InvocationError(f"a duration must be greater than zero, got {raw!r}")
    if unit != "ms" and not n.is_integer():
        raise InvocationError(f"{unit} must be a whole number, got {raw!r}")
    return n


def parse_duration(
    raw: str, *, anchor: str | None = None, clock: int | str | None = None
) -> dict[str, Any]:
    """``4beats`` / ``2bars`` / ``1phrase`` / ``500ms`` -> the AGENT-04 Duration.

    The bounds are the ones ``agent-duration.ts`` enforces: ``n`` must be a
    finite number, positive, and an integer unless the unit is ``ms``. They are
    checked here so a bad duration is refused by the CLI instead of by the page.
    """
    magnitude, unit = _split_duration(raw)
    n = _duration_n(raw, magnitude, unit)
    duration: dict[str, Any] = {"unit": unit, "n": int(n) if unit != "ms" else n}
    if anchor is not None:
        duration["anchor"] = anchor
    if clock is not None:
        duration["clock"] = clock
    return duration


def describe_verbs() -> list[dict[str, Any]]:
    """The ``--list-verbs`` document, one entry per verb."""
    return [
        {
            "verb": verb.name,
            "command": verb.command_type,
            "usage": verb.usage(),
            "positional_arguments": [
                {
                    "key": argument.key,
                    "kind": argument.kind,
                    "optional": argument.optional,
                    "arity": argument.arity,
                }
                for argument in verb.args
            ],
            "fixed": dict(verb.fixed),
            "quick_draw": list(verb.quick_draws),
            "confirmed_against_mirror": bool(verb.observes),
            "rampable": verb.command_type in RAMPABLE_TYPES,
            "note": verb.note,
        }
        for verb in VERBS.values()
    ]
