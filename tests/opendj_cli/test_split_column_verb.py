"""LIBUX (issue #9754): the browser's split-column button is reachable from ``opendj`` as well as the toolbar.

[if] ``opendj library_split_column true`` does not emit the enabled flag [then] agents cannot open the second table.
[if] a non-boolean reaches the bus [then] the CLI hands the browser a value the guard refuses.
"""

from __future__ import annotations

import pytest

from apps.opendj_cli.verbs import InvocationError, parse_invocation


@pytest.mark.parametrize(("raw", "expected"), [("true", True), ("false", False)])
def test_split_column_verb_emits_the_flag(raw: str, expected: bool) -> None:
    """[if] library_split_column gets true or false [then] it emits library_split_column with that flag, [else stop]."""
    assert parse_invocation(["library_split_column", raw]).command == {
        "type": "library_split_column",
        "enabled": expected,
    }


def test_split_column_verb_without_a_value_is_a_flip() -> None:
    """[if] library_split_column has no value [then] it emits the bare command, which the bus treats as a flip, [else stop]."""
    assert parse_invocation(["library_split_column"]).command == {"type": "library_split_column"}


def test_split_column_verb_refuses_a_non_boolean() -> None:
    """[if] library_split_column gets "maybe" [then] the CLI refuses it before dispatch, [else stop]."""
    with pytest.raises(InvocationError):
        parse_invocation(["library_split_column", "maybe"])
