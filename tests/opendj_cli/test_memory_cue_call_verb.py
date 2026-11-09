"""CUES-10013: agent parity for memory cue CALL, `opendj memory_cue_call <deck> prev|next`.

[if] the verb does not build the bus command, or accepts a direction other than prev or next [then] fail, [else stop].
"""

from __future__ import annotations

import pytest

from apps.opendj_cli.verbs import InvocationError, parse_invocation

pytestmark = [pytest.mark.requirement("CUES-10013")]


@pytest.mark.parametrize("direction", ["prev", "next"])
def test_memory_cue_call_builds_the_bus_command(direction: str) -> None:
    """[if] `opendj memory_cue_call 3 <direction>` does not build the bus command [then] fail."""
    assert parse_invocation(["memory_cue_call", "3", direction]).command == {
        "type": "memory_cue_call",
        "deck": 3,
        "direction": direction,
    }


def test_memory_cue_call_refuses_an_unknown_direction() -> None:
    """[if] a direction other than prev or next reaches the bus [then] fail."""
    with pytest.raises(InvocationError, match="prev\\|next"):
        parse_invocation(["memory_cue_call", "1", "back"])
