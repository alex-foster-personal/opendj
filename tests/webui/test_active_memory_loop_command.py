"""CUES-10639: active memory loop changes use the typed performance bus.

[if] the deck loop menu toggles active [then] it dispatches active_memory_loop on the bus, [else stop].
"""

from __future__ import annotations

import pytest

from apps.opendj_cli.verbs import parse_invocation
from tests.opendj_cli import ts_contract

pytestmark = pytest.mark.requirement("CUES-10639")


def test_active_memory_loop_is_a_declared_performance_command() -> None:
    """[if] active_memory_loop is missing from the bus [then] agents cannot queue it, [else stop]."""
    assert "active_memory_loop" in ts_contract.command_fields()


def test_active_memory_loop_verb_builds_the_bus_command() -> None:
    """[if] opendj active_memory_loop is wired [then] it matches the IPC validator, [else stop]."""
    assert parse_invocation(["active_memory_loop", "1", "5000", "7000", "true"]).command == {
        "type": "active_memory_loop",
        "deck": 1,
        "in_ms": 5000.0,
        "out_ms": 7000.0,
        "active": True,
    }
