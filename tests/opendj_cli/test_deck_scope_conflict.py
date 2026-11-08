"""AGENT-9942: a deck scope and a named ``deck=`` that disagree are refused, never guessed.

[if] `opendj deck 1 fader deck=2` builds a command for any deck [then] fail, [else stop].
"""

from __future__ import annotations

import pytest

from apps.opendj_cli.verbs import InvocationError, parse_invocation

pytestmark = [pytest.mark.requirement("AGENT-9942")]


def test_scope_one_with_named_deck_two_is_refused_naming_both_decks() -> None:
    """[if] scope 1 + deck=2 builds a command [then] fail, [else stop]."""
    with pytest.raises(InvocationError, match=r"deck 1 as the scope and deck=2"):
        parse_invocation(["deck", "1", "fader", "deck=2"])


def test_scope_one_with_named_deck_two_is_refused_with_a_value_too() -> None:
    """[if] scope 1 + deck=2 + a value builds a command [then] fail, [else stop]."""
    with pytest.raises(InvocationError, match=r"deck was given twice"):
        parse_invocation(["deck", "1", "fader", "deck=2", "0.5"])


def test_scope_one_with_named_deck_one_is_accepted_once() -> None:
    """[if] scope 1 + deck=1 is refused or moves another deck [then] fail, [else stop]."""
    command = parse_invocation(["deck", "1", "fader", "deck=1", "0.5"]).command
    assert command == {"type": "fader", "deck": 1, "value": 0.5}


def test_the_deck_scope_argument_conflicts_with_a_named_deck() -> None:
    """[if] the CLI deck_scope (not the token prefix) lets deck=2 through [then] fail, [else stop]."""
    with pytest.raises(InvocationError, match=r"deck was given twice"):
        parse_invocation(["fader", "deck=2", "0.5"], deck_scope=1)


def test_non_deck_first_verb_with_conflicting_named_deck_is_refused() -> None:
    """[if] cmdk_pick under scope 3 with deck=2 builds a command [then] fail, [else stop]."""
    with pytest.raises(InvocationError, match=r"deck 3 as the scope and deck=2"):
        parse_invocation(["deck", "3", "cmdk_pick", "0", "deck=2"])


def test_non_deck_first_verb_with_the_same_named_deck_is_accepted_once() -> None:
    """[if] cmdk_pick under scope 3 with deck=3 is refused [then] fail, [else stop]."""
    command = parse_invocation(["deck", "3", "cmdk_pick", "0", "deck=3"]).command
    assert command == {"type": "cmdk_pick", "index": 0, "deck": 3}


@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        (["deck", "1", "fader", "0.5"], {"type": "fader", "deck": 1, "value": 0.5}),
        (["fader", "2", "0.5"], {"type": "fader", "deck": 2, "value": 0.5}),
    ],
)
def test_existing_scope_and_positional_forms_are_unchanged(tokens: list[str], expected: dict[str, object]) -> None:
    """[if] `deck 1 fader 0.5` or `fader 2 0.5` changes [then] fail, [else stop]."""
    assert parse_invocation(tokens).command == expected
