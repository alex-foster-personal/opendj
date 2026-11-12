"""OSC address-to-command map generated from the CLI verb registry.

Every CLI verb in the registry the caller passes in (:data:`apps.opendj_cli.catalog.VERBS`)
is reachable at ``/opendj/<verb>`` with the same command type and argument schema, so OSC
never keeps a second command list. An address that names no verb raises
``UnknownOscAddress``. The registry is injected, read through ``VerbLike``, because this
domain package may not import the CLI delivery layer (.importlinter).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

OSC_ADDRESS_PREFIX: str = "/opendj/"
OSC_RESERVED_CHARACTERS: frozenset[str] = frozenset(" #*,/?[]{}")


class ArgLike(Protocol):
    """One positional argument as the CLI registry declares it (``apps.opendj_cli.kinds.Arg``)."""

    @property
    def key(self) -> str: ...
    @property
    def kind(self) -> str: ...
    @property
    def optional(self) -> bool: ...
    @property
    def arity(self) -> int: ...


class VerbLike(Protocol):
    """One CLI verb (``apps.opendj_cli.catalog.Verb``)."""

    @property
    def name(self) -> str: ...
    @property
    def command_type(self) -> str: ...
    @property
    def args(self) -> Sequence[ArgLike]: ...


class UnknownOscAddress(LookupError):
    """An OSC address names no CLI verb."""


@dataclass(frozen=True)
class OscArgument:
    """One positional argument, as the CLI registry declares it."""

    key: str
    kind: str
    optional: bool
    arity: int


@dataclass(frozen=True)
class OscCommand:
    """The CLI verb an OSC address resolves to."""

    address: str
    verb: str
    command_type: str
    arguments: tuple[OscArgument, ...]


# -----------------------------------------------------------------------------


def osc_address_for(verb_name: str) -> str:
    """Return the OSC address for a CLI verb name."""
    if not verb_name or OSC_RESERVED_CHARACTERS.intersection(verb_name):
        raise ValueError(f"CLI verb {verb_name!r} cannot be an OSC address part")
    return OSC_ADDRESS_PREFIX + verb_name


def _command_for(verb: VerbLike) -> OscCommand:
    return OscCommand(
        address=osc_address_for(verb.name),
        verb=verb.name,
        command_type=verb.command_type,
        arguments=tuple(OscArgument(a.key, a.kind, a.optional, a.arity) for a in verb.args),
    )


def build_command_map(verbs: Mapping[str, VerbLike]) -> dict[str, OscCommand]:
    """Return ``{address: OscCommand}`` with one entry per CLI verb."""
    commands = [_command_for(verb) for verb in verbs.values()]
    command_map = {command.address: command for command in commands}
    if len(command_map) != len(commands):
        raise ValueError("two CLI verbs map to the same OSC address")
    return command_map


def resolve_osc_address(address: str, command_map: Mapping[str, OscCommand]) -> OscCommand:
    """Return the command for ``address``; raise ``UnknownOscAddress`` for anything else."""
    try:
        return command_map[address]
    except KeyError as error:
        raise UnknownOscAddress(f"no CLI verb at OSC address {address!r}") from error


def registry_coverage(command_map: Mapping[str, OscCommand], verbs: Mapping[str, VerbLike]) -> float:
    """Return the fraction of CLI verbs that have an OSC address in ``command_map``."""
    if not verbs:
        raise ValueError("the CLI verb registry is empty")
    mapped = {command.verb for command in command_map.values()}
    return sum(1 for name in verbs if name in mapped) / len(verbs)
