"""LIBM-82b (issue #12317): the track grid's type-ahead jump is reachable from ``opendj`` as well as the keyboard.

[if] ``opendj browser_type_ahead <prefix>`` does not carry the prefix whole [then ⛔] agents jump somewhere else.
[if] an empty prefix reaches the bus [then ⛔] the CLI hands the browser a jump that matches nothing.
"""

from __future__ import annotations

import pytest

from apps.opendj_cli.verbs import InvocationError, parse_invocation


def test_type_ahead_verb_carries_the_prefix_whole() -> None:
    """[if] browser_type_ahead gets "blue mon" [then] it emits browser_type_ahead with that exact prefix, [else stop]."""
    assert parse_invocation(["browser_type_ahead", "blue mon"]).command == {
        "type": "browser_type_ahead",
        "prefix": "blue mon",
    }


@pytest.mark.parametrize("argv", [["browser_type_ahead"], ["browser_type_ahead", ""]])
def test_type_ahead_verb_refuses_a_missing_or_empty_prefix(argv: list[str]) -> None:
    """[if] browser_type_ahead has no prefix or an empty one [then] the CLI refuses it before dispatch, [else stop]."""
    with pytest.raises(InvocationError):
        parse_invocation(argv)
