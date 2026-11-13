"""Issue #10043: the Track Filter bar's state is reachable from ``opendj`` as well as the UI.

[if] ``opendj library_filter`` does not build a get command [then ⛔] agents cannot read the bar.
[if] ``opendj library_filter_set`` drops or rewrites the JSON state [then ⛔] agents cannot set it.
[if] a non-object state reaches the bus [then ⛔] the CLI hands the browser a guess.
"""

from __future__ import annotations

import pytest

from apps.opendj_cli.verbs import InvocationError, parse_invocation


def test_library_filter_builds_the_get_command() -> None:
    """[if] library_filter runs bare [then] it emits type library_filter with op get only, [else stop]."""
    assert parse_invocation(["library_filter"]).command == {"type": "library_filter", "op": "get"}


def test_library_filter_set_carries_the_json_state_whole() -> None:
    """[if] library_filter_set gets a BPM range and suspended [then] the command carries that object as is, [else stop]."""
    state = '{"bpm":{"min":120,"max":128},"key":{"selected":["8A","9A"]},"suspended":true}'
    assert parse_invocation(["library_filter_set", state]).command == {
        "type": "library_filter",
        "op": "set",
        "state": {"bpm": {"min": 120, "max": 128}, "key": {"selected": ["8A", "9A"]}, "suspended": True},
    }


@pytest.mark.parametrize("raw", ["[1, 2]", "120", "{not json"])
def test_library_filter_set_refuses_a_state_that_is_not_a_json_object(raw: str) -> None:
    """[if] the state token is a list, a number or broken JSON [then] the CLI refuses it before dispatch, [else stop]."""
    with pytest.raises(InvocationError, match="must be a JSON object"):
        parse_invocation(["library_filter_set", raw])
