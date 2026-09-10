"""AGENT-05 accepts only if the verb table and the bus cannot disagree.

[if] a command exists on the bus [then ⛔] ``opendj --list-verbs`` names it
     (REQUIREMENTS.md:4195, and PARITY-07 clause (2) at line 4416).
[if] a verb exists [then ⛔] the bus declares that command type, so the CLI
     cannot invent one.
[if] a verb builds a command [then ⛔] every key it emits is a key the wire
     validator accepts, because ``_exactKeys`` throws on an unexpected field.
[if] a quick-draw action exists [then ⛔] a verb reaches the same command, which
     is what "verb ids seeded from QuickDrawActionId" means in practice.
"""
from __future__ import annotations

import pytest

from apps.opendj_cli.catalog import RAMPABLE_TYPES, VERBS
from apps.opendj_cli.verbs import (
    DURATION_ANCHORS,
    InvocationError,
    describe_verbs,
    parse_duration,
    parse_invocation,
)
from tests.opendj_cli import ts_contract

# A token per value kind, tried in order: the first that parses is the sample.
# Derived by asking the verb's own parser rather than by restating the domain,
# so a narrowed domain cannot leave this list quietly wrong.
_SAMPLE_TOKENS = (
    "1", "8", "true", "0.5", "sample", "0 1000",
    "A", "bad", "beat", "low", "next", "top", "vocal",
)


def _sample(verb_name: str, arg_key: str) -> str:
    arg = next(arg for arg in VERBS[verb_name].args if arg.key == arg_key)
    for token in _SAMPLE_TOKENS:
        try:
            arg.parse(arg.key, token)
        except ValueError:
            continue
        return token
    raise AssertionError(f"no sample token parses for {verb_name} <{arg_key}>")


def _sample_tokens(verb_name: str) -> list[str]:
    tokens: list[str] = []
    for arg in VERBS[verb_name].args:
        tokens.extend(_sample(verb_name, arg.key).split(" "))
    return tokens


def test_every_command_on_the_bus_has_a_verb() -> None:
    """The acceptance criterion, read straight off the wire contract."""

    declared = set(ts_contract.command_fields())
    named = {verb.command_type for verb in VERBS.values()}
    assert declared - named == set(), "commands with no opendj verb"


def test_no_verb_names_a_command_the_bus_does_not_declare() -> None:
    declared = set(ts_contract.command_fields())
    named = {verb.command_type for verb in VERBS.values()}
    assert named - declared == set(), "verbs naming a command that does not exist"


def test_list_verbs_names_every_command_on_the_bus() -> None:
    listed = {row["command"] for row in describe_verbs()}
    assert listed == set(ts_contract.command_fields())


@pytest.mark.parametrize("verb_name", sorted(VERBS))
def test_every_verb_emits_only_fields_the_wire_validator_accepts(verb_name: str) -> None:
    verb = VERBS[verb_name]
    declared = ts_contract.command_fields()[verb.command_type]
    command = parse_invocation([verb_name, *_sample_tokens(verb_name)]).command
    assert set(command) - set(declared) - {"type"} == set(), "a field _exactKeys would reject"
    required = {key for key, optional in declared.items() if not optional}
    assert required - set(command) == set(), "a field the command must carry"


@pytest.mark.parametrize("verb_name", sorted(VERBS))
def test_every_verb_round_trips_through_the_parser(verb_name: str) -> None:
    """The parser is exercised for every verb, not just the documented four."""

    invocation = parse_invocation([verb_name, *_sample_tokens(verb_name)])
    assert invocation.command["type"] == VERBS[verb_name].command_type


def test_every_quick_draw_action_is_reachable_from_a_verb() -> None:
    reachable = {action for verb in VERBS.values() for action in verb.quick_draws}
    assert set(ts_contract.quick_draw_ids()) - reachable == set()


def test_deck_scope_and_positional_deck_build_the_same_command() -> None:
    assert parse_invocation(["deck", "1", "play"]) == parse_invocation(["play", "1"])


def test_naming_the_deck_twice_is_refused_rather_than_guessed() -> None:
    with pytest.raises(InvocationError, match="twice"):
        parse_invocation(["play", "1"], deck_scope=2)


def test_a_trailing_deck_scope_fills_the_verbs_own_deck_field() -> None:
    scoped = parse_invocation(["eq", "low", "0.5"], deck_scope=3)
    assert scoped.command == {"type": "eq", "deck": 3, "band": "low", "value": 0.5}


def test_an_unknown_verb_is_refused_by_name() -> None:
    with pytest.raises(InvocationError, match="unknown verb 'scratch'"):
        parse_invocation(["scratch", "1"])


def test_a_control_value_outside_the_bus_domain_is_refused_before_dispatch() -> None:
    """The example in REQUIREMENTS.md is -0.3 and the bus domain is 0..1.

    Sending it would be a round trip to a page that can only answer
    "value must be within 0..1", so the refusal happens here and says why.
    """

    with pytest.raises(InvocationError, match=r"within 0\.\.1"):
        parse_invocation(["eq", "2", "low", "-0.3"])


def test_leftover_tokens_are_refused_rather_than_dropped() -> None:
    with pytest.raises(InvocationError, match="does not take"):
        parse_invocation(["cue", "1", "extra"])


def test_a_loop_needs_both_of_its_numbers() -> None:
    assert parse_invocation(["loop", "1", "1000", "4000"]).command["loop"] == {
        "in_ms": 1000.0,
        "out_ms": 4000.0,
    }
    with pytest.raises(InvocationError, match="in_ms then out_ms"):
        parse_invocation(["loop", "1", "1000"])


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("4beats", {"unit": "beats", "n": 4.0}),
        ("4beats", {"unit": "beats", "n": 4.0}),
        ("1bar", {"unit": "bars", "n": 1.0}),
        ("2bars", {"unit": "bars", "n": 2.0}),
        ("3phrases", {"unit": "phrases", "n": 3.0}),
        ("500ms", {"unit": "ms", "n": 500.0}),
    ],
)
def test_duration_units_resolve_as_agent_04_declares_them(raw: str, expected: dict) -> None:
    assert parse_duration(raw) == expected


def test_a_duration_without_a_unit_is_refused() -> None:
    with pytest.raises(InvocationError, match="--over needs a duration"):
        parse_duration("4")


def test_the_rampable_types_match_the_pages_own_ramp_guard() -> None:
    """agent-orders.ts refuses a ramp over anything but these four."""

    assert {"eq", "fader", "trim", "filter"} == RAMPABLE_TYPES


def test_anchors_are_the_three_the_duration_type_declares() -> None:
    assert DURATION_ANCHORS == ("next_beat", "next_downbeat", "next_phrase")
