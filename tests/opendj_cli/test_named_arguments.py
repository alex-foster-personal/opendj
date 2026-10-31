"""Named ``key=value`` arguments let a caller skip an optional field without inventing the ones before it."""

from __future__ import annotations

import pytest

from apps.opendj_cli.verbs import InvocationError, parse_invocation


def test_a_named_deck_and_confirm_reach_their_own_slots_without_an_id() -> None:
    command = parse_invocation(["cmdk_pick", "0", "deck=2", "confirm=true"]).command
    assert command == {"type": "cmdk_pick", "index": 0, "deck": 2, "confirm": True}


def test_a_positional_id_still_fills_the_first_free_slot() -> None:
    # The reviewer's case: `cmdk_pick 0 2` keeps binding 2 to id, which is the documented positional rule.
    command = parse_invocation(["cmdk_pick", "0", "2"]).command
    assert command.get("id") == "2"
    assert "deck" not in command


def test_a_deck_scope_prefix_lands_on_the_deck_slot_when_deck_is_not_first() -> None:
    command = parse_invocation(["deck", "3", "cmdk_pick", "0"]).command
    assert command == {"type": "cmdk_pick", "index": 0, "deck": 3}


def test_a_deck_given_both_as_scope_and_by_name_is_refused() -> None:
    with pytest.raises(InvocationError, match="deck was given twice"):
        parse_invocation(["deck", "3", "cmdk_pick", "0", "deck=2"])


def test_a_named_token_twice_is_refused() -> None:
    with pytest.raises(InvocationError, match="deck was given twice"):
        parse_invocation(["cmdk_pick", "0", "deck=1", "deck=2"])


def test_a_bad_named_value_names_its_argument() -> None:
    with pytest.raises(InvocationError):
        parse_invocation(["cmdk_pick", "0", "deck=9"])


def test_a_token_whose_key_names_no_argument_stays_positional() -> None:
    # cmdk_open's query is text: `a=b` is the query, not an unknown named argument.
    command = parse_invocation(["cmdk_open", "artist=Bonobo"]).command
    assert command.get("query") == "artist=Bonobo"
