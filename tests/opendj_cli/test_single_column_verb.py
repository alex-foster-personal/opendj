"""LIBUX (issue #9743): the browser's single-column layout is reachable from ``opendj`` as well as the button.

[if] ``opendj library_single_column true`` does not emit the enabled flag [then ⛔] agents cannot hide the tree.
[if] the bare verb carries an enabled value [then ⛔] the flip form forces one state instead of toggling.
[if] a non-boolean reaches the bus [then ⛔] the CLI hands the browser a layout value it refuses.
"""

from __future__ import annotations

import pytest

from apps.opendj_cli.verbs import InvocationError, parse_invocation


@pytest.mark.parametrize(("raw", "enabled"), [("true", True), ("false", False)])
def test_single_column_verb_carries_the_enabled_flag(raw: str, enabled: bool) -> None:
    """[if] library_single_column gets true or false [then] it emits library_single_column with that flag, [else stop]."""
    assert parse_invocation(["library_single_column", raw]).command == {
        "type": "library_single_column",
        "enabled": enabled,
    }


def test_single_column_verb_without_a_value_flips() -> None:
    """[if] library_single_column has no value [then] it emits the bare command, which the bus treats as a flip, [else stop]."""
    assert parse_invocation(["library_single_column"]).command == {"type": "library_single_column"}


def test_single_column_verb_refuses_a_non_boolean() -> None:
    """[if] library_single_column gets "maybe" [then] the CLI refuses it before dispatch, [else stop]."""
    with pytest.raises(InvocationError):
        parse_invocation(["library_single_column", "maybe"])
