"""AGENT-03 order documents, and the ``do ... then ... and ...`` script grammar.

The four order kinds are the bus's own (``single``, ``sequence``, ``parallel``,
``ramp``); this module only builds them. The script grammar is deliberately
small: ``then`` separates steps that must not overlap, ``and`` groups commands
the caller declares may run together. A group of one command is a ``single``
order and a group of several is a ``parallel`` one, and groups run one after
another with the sequence rule the bus already states - stop on the first
error and mark the rest skipped.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from apps.opendj_cli.catalog import RAMPABLE_TYPES
from apps.opendj_cli.verbs import Invocation, InvocationError, parse_invocation

SINGLE = "single"
SEQUENCE = "sequence"
PARALLEL = "parallel"
RAMP = "ramp"

SEQUENCE_WORD = "then"
PARALLEL_WORD = "and"
_SEPARATORS = (SEQUENCE_WORD, PARALLEL_WORD)


def single(invocation: Invocation) -> dict[str, Any]:
    return {SINGLE: invocation.command}


def parallel(invocations: Sequence[Invocation]) -> dict[str, Any]:
    return {PARALLEL: [invocation.command for invocation in invocations]}


def sequence(invocations: Sequence[Invocation]) -> dict[str, Any]:
    return {SEQUENCE: [invocation.command for invocation in invocations]}


def ramp(invocation: Invocation, over: dict[str, Any]) -> dict[str, Any]:
    """One control ramp: the verb's own value is the target it travels to."""
    command = invocation.command
    if command["type"] not in RAMPABLE_TYPES:
        raise InvocationError(
            f"--over ramps eq, fader, trim or filter; {command['type']} is not one "
            "of them (the page refuses any other command type)"
        )
    return {RAMP: {"command": command, "to": command["value"], "over": over}}


@dataclass(frozen=True)
class Group:
    """One bus order built from one or more verb invocations."""

    kind: str
    invocations: tuple[Invocation, ...]

    def order(self) -> dict[str, Any]:
        if self.kind == SINGLE:
            return single(self.invocations[0])
        if self.kind == PARALLEL:
            return parallel(self.invocations)
        raise AssertionError(f"unknown group kind {self.kind}")

    def label(self) -> str:
        return " and ".join(_label(invocation) for invocation in self.invocations)


def _label(invocation: Invocation) -> str:
    parts = [invocation.verb.name]
    parts.extend(
        str(invocation.values[argument.key])
        for argument in invocation.verb.args
        if argument.key in invocation.values
    )
    return " ".join(parts)


def _reject_stray_separators(items: Sequence[str]) -> None:
    """``then``/``and`` are infixes: one at either end joins nothing."""
    for index, item in enumerate(items):
        if item not in _SEPARATORS:
            continue
        if index == 0 or index == len(items) - 1 or items[index + 1] in _SEPARATORS:
            raise InvocationError(
                f"`{item}` must sit between two quoted commands, as in: "
                'opendj do "load 1 x" then "play 1" and "play 2"'
            )


def _segments(items: Sequence[str]) -> list[list[str]]:
    """Group the quoted commands: ``and`` joins, anything else starts a step."""
    segments: list[list[str]] = []
    for index, item in enumerate(items):
        if item in _SEPARATORS:
            continue
        if index > 0 and items[index - 1] == PARALLEL_WORD and segments:
            segments[-1].append(item)
        else:
            segments.append([item])
    return segments


def parse_script(items: Sequence[str], deck_scope: int | None = None) -> tuple[Group, ...]:
    """Parse the tokens after ``do`` into the groups to run, in order."""
    if not items:
        raise InvocationError("do needs at least one quoted command")
    _reject_stray_separators(items)
    groups = []
    for segment in _segments(items):
        invocations = tuple(parse_invocation(tokens.split(), deck_scope) for tokens in segment)
        kind = SINGLE if len(invocations) == 1 else PARALLEL
        groups.append(Group(kind=kind, invocations=invocations))
    return tuple(groups)


__all__ = [
    "PARALLEL",
    "RAMP",
    "SEQUENCE",
    "SINGLE",
    "Group",
    "parallel",
    "parse_script",
    "ramp",
    "sequence",
    "single",
]
