"""LIBUX (issue #10026): the browser's My Tag pane toggle is reachable from ``opendj`` as well as the toolbar.

[if] ``opendj library_mytag_pane true`` does not emit the enabled flag [then] agents cannot open the pane.
[if] the bare verb carries an enabled value [then] the flip form forces one state instead of toggling.
[if] a non-boolean reaches the bus [then] the CLI hands the browser a value the guard refuses.
"""

from __future__ import annotations

import pytest

from apps.opendj_cli.verbs import InvocationError, parse_invocation


@pytest.mark.parametrize(("raw", "enabled"), [("true", True), ("false", False)])
def test_mytag_pane_verb_carries_the_enabled_flag(raw: str, enabled: bool) -> None:
    """[if] library_mytag_pane gets true or false [then] it emits library_mytag_pane with that flag, [else stop]."""
    assert parse_invocation(["library_mytag_pane", raw]).command == {"type": "library_mytag_pane", "enabled": enabled}


def test_mytag_pane_verb_without_a_value_flips() -> None:
    """[if] library_mytag_pane has no value [then] it emits the bare command, which the bus treats as a flip, [else stop]."""
    assert parse_invocation(["library_mytag_pane"]).command == {"type": "library_mytag_pane"}


def test_mytag_pane_verb_refuses_a_non_boolean() -> None:
    """[if] library_mytag_pane gets "maybe" [then] the CLI refuses it before dispatch, [else stop]."""
    with pytest.raises(InvocationError):
        parse_invocation(["library_mytag_pane", "maybe"])
