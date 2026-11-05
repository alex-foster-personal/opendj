"""KEYSYNC-01 agent parity: deck_key_sync and deck_key_options.

- if deck_key_sync does not send the same key_sync order the button sends then agents and the UI diverge
- if deck_key_sync with shift does not send key_shift then a ladder row cannot be picked by an agent
- if deck_key_options recomputes the ladder instead of reading the page's then the two can disagree
- if a bad argument reaches the page then an agent typo can move a live deck
- if the CLI verbs drift from the wire then `opendj key_sync`/`key_shift` refuse at runtime

-Claude
"""

from __future__ import annotations

import pytest

from apps.opendj_cli.catalog import VERBS
from apps.opendj_cli.mcp_key_tools import key_options_from_mirror, key_sync_order
from apps.opendj_cli.verbs import parse_invocation


def test_key_sync_order_defaults_to_exact_onto_master() -> None:
    assert key_sync_order(2, "master", "exact", None) == {
        "type": "key_sync", "deck": 2, "enabled": True, "mode": "exact", "target": "master",
    }
    assert key_sync_order(1, 3, "relative", None)["target"] == 3


def test_key_sync_order_with_shift_is_an_absolute_key_shift() -> None:
    assert key_sync_order(1, "master", "exact", 2) == {"type": "key_shift", "deck": 1, "semitones": 2}
    assert key_sync_order(1, "master", "exact", 0)["semitones"] == 0


@pytest.mark.parametrize(
    ("deck", "target", "mode", "shift"),
    [(5, "master", "exact", None), (1, 1, "exact", None), (1, 9, "exact", None),
     (1, "master", "loose", None), (1, "master", "exact", 13), (1, "master", "exact", True)],
)
def test_key_sync_order_refuses_bad_arguments(deck: int, target: int | str, mode: str, shift: int | None) -> None:
    with pytest.raises(ValueError):
        key_sync_order(deck, target, mode, shift)


ROWS = [{"shift": 1, "key": "3A", "pattern": "exact", "desirable": True}]


def test_key_options_read_the_page_ladder_and_resolve_master() -> None:
    mirror = {
        "master_deck": 2,
        "decks": {"1": {"key_sync": {"options": {"2": ROWS}}}, "2": {"key_sync": {"options": {}}}},
    }
    assert key_options_from_mirror(mirror, 1, 2) == ROWS
    assert key_options_from_mirror(mirror, 1, "master") == ROWS
    with pytest.raises(ValueError, match="no ladder"):
        key_options_from_mirror(mirror, 1, 3)
    with pytest.raises(TypeError, match="older page"):
        key_options_from_mirror({"master_deck": None, "decks": {"1": {}}}, 1, 2)
    with pytest.raises(ValueError, match="no MASTER"):
        key_options_from_mirror({"master_deck": None, "decks": {"1": {"key_sync": {"options": {}}}}}, 1, "master")


def test_cli_verbs_build_the_wire_orders() -> None:
    assert "key_shift" in VERBS
    order = parse_invocation(["key_sync", "2", "true", "relative", "3"]).command
    assert order == {"type": "key_sync", "deck": 2, "enabled": True, "mode": "relative", "target": 3}
    assert parse_invocation(["key_sync", "2", "true"]).command == {"type": "key_sync", "deck": 2, "enabled": True}
    assert parse_invocation(["key_shift", "1", "-3"]).command == {"type": "key_shift", "deck": 1, "semitones": -3}
