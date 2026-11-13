"""``opendj browser_tree_search``: the CLI twin of the playlist tree's search box (LIBM-87, issue #12319).

[if] an agent runs `opendj browser_tree_search query=loft` [then] the bus command filters the tree, [else stop].

Regression one-liners:
  - if `browser_tree_search` accepts an empty query then an agent clears by accident instead of by name
  - if `browser_tree_search_clear` takes arguments then the clear verb is not the plain way out
"""

from __future__ import annotations

import pytest

from apps.opendj_cli.verbs import InvocationError, parse_invocation

pytestmark = pytest.mark.requirement("LIBM-13961")


def test_tree_search_builds_the_bus_command() -> None:
    """[if] browser_tree_search gets a name fragment [then] the command carries it whole, [else stop]."""
    assert parse_invocation(["browser_tree_search", "query=Fabric Loft"]).command == {
        "type": "browser_tree_search",
        "query": "Fabric Loft",
    }


def test_tree_search_clear_builds_the_bus_command() -> None:
    """[if] browser_tree_search_clear runs [then] the command clears with no fields, [else stop]."""
    assert parse_invocation(["browser_tree_search_clear"]).command == {"type": "browser_tree_search_clear"}


def test_tree_search_refuses_an_empty_query() -> None:
    """[if] browser_tree_search gets no or an empty query [then] the CLI refuses it, [else stop]."""
    with pytest.raises(InvocationError):
        parse_invocation(["browser_tree_search"])
    with pytest.raises(InvocationError):
        parse_invocation(["browser_tree_search", "query="])
