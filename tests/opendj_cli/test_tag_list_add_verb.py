"""Issue #10028: the row menu's Add to Tag List is reachable from ``opendj`` as well as the menu.

[if] ``opendj tag_list_add a,b`` does not carry every id in order [then ⛔] agents add the wrong tracks.
[if] an empty id reaches the bus [then ⛔] the browser posts an add the route refuses.
"""

from __future__ import annotations

import pytest

from apps.opendj_cli.verbs import InvocationError, parse_invocation


def test_tag_list_add_verb_carries_every_id_in_order() -> None:
    """[if] tag_list_add gets "fixture:2,fixture:1" [then] it emits tag_list_add with both ids in that order, [else stop]."""
    assert parse_invocation(["tag_list_add", "fixture:2,fixture:1"]).command == {
        "type": "tag_list_add",
        "stable_ids": ["fixture:2", "fixture:1"],
    }


@pytest.mark.parametrize("argv", [["tag_list_add"], ["tag_list_add", ""], ["tag_list_add", "a,,b"]])
def test_tag_list_add_verb_refuses_a_missing_or_empty_id(argv: list[str]) -> None:
    """[if] tag_list_add has no ids or an empty one [then] the CLI refuses it before dispatch, [else stop]."""
    with pytest.raises(InvocationError):
        parse_invocation(argv)
