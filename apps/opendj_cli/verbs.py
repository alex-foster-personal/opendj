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
from apps.opendj_cli.kinds import DECK_VALUES, Arg, deck_value, text_value

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


def _named_tokens(args: Sequence[Arg], rest: Sequence[str]) -> tuple[dict[str, str], list[str]]:
    """Split out ``key=value`` tokens that name a one-token argument of the verb.

    Positional tokens fill slots in order, so ``cmdk_pick 0 2`` binds 2 to ``id``.
    A named token reaches its slot directly, so ``cmdk_pick 0 deck=2`` targets the
    deck without inventing an ``id``. A token whose key names no argument stays
    positional, so text that happens to contain ``=`` is not refused.
    """
    keys = {argument.key for argument in args if argument.arity == 1}
    named: dict[str, str] = {}
    positional: list[str] = []
    for token in rest:
        key, separator, value = token.partition("=")
        if separator and key in keys:
            if key in named:
                raise InvocationError(f"{key} was given twice; give it once")
            named[key] = value
        else:
            positional.append(token)
    return named, positional


def _consume(
    args: Sequence[Arg], rest: Sequence[str], named: Mapping[str, str] | None = None
) -> tuple[dict[str, Any], list[str]]:
    values: dict[str, Any] = {}
    remaining = list(rest)
    named = named or {}
    for argument in args:
        if argument.key in named:
            try:
                values[argument.key] = argument.parse(argument.key, named[argument.key])
            except ValueError as error:
                raise InvocationError(str(error)) from error
            continue
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


def _drop_named_deck_matching_scope(scoped: int | None, positionals: list[str]) -> list[str]:
    """AGENT-9708: a named ``deck=`` beside a deck scope must name the same deck.

    Checked before positional binding: otherwise the scope deck is inserted as the
    first positional, the named deck overrides it, and the scope number slides into
    the next argument (``deck 1 fader deck=2`` moved deck 2's fader to full). The
    same deck named twice is accepted once; a different one is refused.
    """
    if scoped is None:
        return positionals
    kept: list[str] = []
    for token in positionals:
        key, separator, raw = token.partition("=")
        if not (separator and key == "deck"):
            kept.append(token)
            continue
        try:
            named = deck_value("deck", raw)
        except ValueError as error:
            raise InvocationError(str(error)) from error
        if named != scoped:
            raise InvocationError(
                f"deck was given twice (deck {scoped} as the scope and deck={named}); "
                "give it once, either as `deck <n> <verb>` or after the verb"
            )
    return kept


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
    if verb.args[0].key == "deck":
        return verb, [str(scoped), *positionals]
    # The deck is not the first argument (cmdk_pick's index comes first), so the
    # scope prefix names it rather than guessing the slot it would have filled.
    return verb, [*positionals, f"deck={scoped}"]


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


def _parse_track_info_open(scoped: int | None, positionals: list[str]) -> Invocation:
    """``track_info_open`` targets exactly one deck or one library stable_id (#9448)."""
    verb = VERBS["track_info_open"]
    named, rest = _named_tokens(verb.args, positionals)
    if scoped is not None:
        if "deck" in named and deck_value("deck", named["deck"]) != scoped:
            raise InvocationError(
                f"deck was given twice (deck {scoped} as the scope and deck={named['deck']}); "
                "give it once, either as `deck <n> <verb>` or after the verb"
            )
        named = {**named, "deck": str(scoped)}
    if "deck" in named and "stable_id" in named:
        raise InvocationError("track_info_open takes deck or stable_id, not both")
    if "deck" in named:
        deck = deck_value("deck", named["deck"])
        if rest:
            _reject_leftover(verb, {"deck": deck}, rest, takes_deck=False)
        command = verb.build({"deck": deck})
        return Invocation(verb=verb, command=command, values={"deck": deck})
    if "stable_id" in named:
        stable_id = text_value("stable_id", named["stable_id"])
        if rest:
            _reject_leftover(verb, {"stable_id": stable_id}, rest, takes_deck=False)
        command = verb.build({"stable_id": stable_id})
        return Invocation(verb=verb, command=command, values={"stable_id": stable_id})
    if len(rest) == 1:
        token = rest[0]
        try:
            deck = deck_value("deck", token)
        except ValueError:
            stable_id = text_value("stable_id", token)
            command = verb.build({"stable_id": stable_id})
            return Invocation(verb=verb, command=command, values={"stable_id": stable_id})
        command = verb.build({"deck": deck})
        return Invocation(verb=verb, command=command, values={"deck": deck})
    if not rest and not named:
        raise InvocationError("track_info_open needs deck or stable_id")
    raise InvocationError(
        "track_info_open takes one deck or one stable_id; use deck= or stable_id= when naming both"
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
    positionals = _drop_named_deck_matching_scope(scoped, positionals)
    if name == "track_info_open":
        return _parse_track_info_open(scoped, positionals)
    verb, positionals = _verb_for(name, scoped, positionals)
    named, positionals = _named_tokens(verb.args, positionals)
    values, leftover = _consume(verb.args, positionals, named)
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
