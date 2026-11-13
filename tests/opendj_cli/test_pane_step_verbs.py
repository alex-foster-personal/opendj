"""Issue #9744: the pane tab's previous and next buttons are reachable from ``opendj`` as well as the UI.

[if] ``opendj browser_next_track`` does not build a step of 1 [then ⛔] agents cannot step the selection down.
[if] ``opendj browser_prev_track`` does not build a step of -1 [then ⛔] agents cannot step the selection up.
[if] either verb takes an argument [then ⛔] the CLI hands the browser a delta the bus refuses.
"""

from __future__ import annotations

import pytest

from apps.opendj_cli.verbs import InvocationError, parse_invocation


@pytest.mark.parametrize(("verb", "delta"), [("browser_next_track", 1), ("browser_prev_track", -1)])
def test_pane_step_verbs_build_the_step_command(verb: str, delta: int) -> None:
    """[if] browser_next_track or browser_prev_track runs bare [then] it emits browser_step_selection with that delta, [else stop]."""
    assert parse_invocation([verb]).command == {"type": "browser_step_selection", "delta": delta}


def test_pane_step_verbs_take_no_argument() -> None:
    """[if] browser_next_track gets a stray argument [then] the CLI refuses it before dispatch, [else stop]."""
    with pytest.raises(InvocationError):
        parse_invocation(["browser_next_track", "5"])
