"""OSC command map parity with the CLI verb registry (AGENT-12238).

[if] the CLI registry exposes a verb [then] OSC maps it with the same schema, [else stop].
"""

from __future__ import annotations

import pytest

from apps.opendj_cli.catalog import VERBS
from apps.osc.command_map import (
    UnknownOscAddress,
    build_command_map,
    osc_address_for,
    registry_coverage,
    resolve_osc_address,
)

pytestmark = pytest.mark.requirement("AGENT-12238")

OSC_COMMAND_MAP = build_command_map(VERBS)


def test_every_registry_verb_has_an_address() -> None:
    """[if] the CLI registry exposes a verb [then] the OSC map has its address, [else stop]."""
    assert VERBS, "the CLI verb registry must not be empty"
    missing = [name for name in VERBS if osc_address_for(name) not in OSC_COMMAND_MAP]
    assert missing == []
    assert len(OSC_COMMAND_MAP) == len(VERBS)


@pytest.mark.parametrize("name", sorted(VERBS))
def test_resolved_command_matches_registry(name: str) -> None:
    """[if] an address resolves [then] command type and argument schema equal the registry's, [else stop]."""
    verb = VERBS[name]
    command = resolve_osc_address(f"/opendj/{name}", OSC_COMMAND_MAP)
    assert command.verb == verb.name
    assert command.command_type == verb.command_type
    assert [(a.key, a.kind, a.optional, a.arity) for a in command.arguments] == [
        (a.key, a.kind, a.optional, a.arity) for a in verb.args
    ]


@pytest.mark.parametrize("address", ["/opendj/not_a_verb", "/opendj/", "/play", "", "/opendj/play/extra"])
def test_unknown_address_fails_closed(address: str) -> None:
    """[if] an unknown OSC address arrives [then] resolving raises, no fallback, [else stop]."""
    with pytest.raises(UnknownOscAddress):
        resolve_osc_address(address, OSC_COMMAND_MAP)


def test_registry_coverage_is_complete() -> None:
    """[if] registry coverage is measured [then] it is exactly 100 percent, [else stop]."""
    assert registry_coverage(OSC_COMMAND_MAP, VERBS) == 1.0
    partial = dict(list(build_command_map(VERBS).items())[:-1])
    assert registry_coverage(partial, VERBS) < 1.0
