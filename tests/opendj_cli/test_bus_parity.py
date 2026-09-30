"""AGENT-05 accepts only if the verb table and the bus cannot disagree.

[if] a command exists on the bus [then ⛔] ``opendj --list-verbs`` names it
     (REQUIREMENTS.md:4195, and PARITY-07 clause (2) at line 4416).
[if] a verb exists [then ⛔] the bus declares that command type, so the CLI
     cannot invent one.
[if] a verb builds a command [then ⛔] every key it emits is a key the wire
     validator accepts, because ``_exactKeys`` throws on an unexpected field.
[if] a quick-draw action exists [then ⛔] a verb reaches the same command, which
     is what "verb ids seeded from QuickDrawActionId" means in practice.
[if] a verb observes the mirror [then ⛔] its expectation is either a sentinel
     or a field the verb's own command carries, so an observation cannot name a
     field that will never be built.
[if] a verb observes a deck path [then ⛔] ``buildUiMirror`` publishes that key,
     read from the live source, so an observation cannot read a field the
     mirror does not carry.
[if] a scripted command quotes a text value containing spaces [then ⛔] it
     reaches the bus whole, exactly as the same value does on the command line.
"""
from __future__ import annotations

import pytest

from apps.opendj_cli.catalog import RAMPABLE_TYPES, VERBS, Expectation
from apps.opendj_cli.orders import parse_script
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
    "1", "8", "true", "0.5", "sample", "0 1000", "1:5000",
    "A", "bad", "beat", "low", "next", "top", "vocal",
    "beatgrid", "own", "practice", "hybrid", "tri-band",
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


def test_head_delay_ms_uses_the_0_to_500_ms_domain_not_unit() -> None:
    assert parse_invocation(["head_delay_ms", "40"]).command == {
        "type": "head_delay_ms",
        "value": 40.0,
    }
    with pytest.raises(InvocationError, match=r"0\.\.500"):
        parse_invocation(["head_delay_ms", "501"])


def test_waveform_seek_builds_the_bus_command_with_snap() -> None:
    assert parse_invocation(["waveform_seek", "2", "12500", "downbeat"]).command == {
        "type": "waveform_seek",
        "deck": 2,
        "position_ms": 12500.0,
        "snap": "downbeat",
    }
    with pytest.raises(InvocationError, match="downbeat\\|beat\\|exact"):
        parse_invocation(["waveform_seek", "1", "0", "instant"])


def test_set_waveform_design_builds_the_bus_command() -> None:
    assert parse_invocation(["set_waveform_design", "tri-band"]).command == {
        "type": "set_waveform_design",
        "design": "tri-band",
    }
    with pytest.raises(InvocationError, match="tri-band\\|mono\\|line"):
        parse_invocation(["set_waveform_design", "rainbow"])


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

    assert {"eq", "fader", "trim", "filter", "stem_gain"} == RAMPABLE_TYPES


def test_anchors_are_the_three_the_duration_type_declares() -> None:
    assert DURATION_ANCHORS == ("next_beat", "next_downbeat", "next_phrase")


# ----- what a verb may claim to observe ------------------------------------

@pytest.mark.parametrize("verb_name", sorted(VERBS))
def test_every_observation_expects_a_sentinel_or_a_field_the_command_carries(
    verb_name: str,
) -> None:
    """An observation naming a field the command never emits can never pass.

    ``unload`` shipped with the wrong half of an overloaded ``expect=None``,
    which meant "must be present and not null" for a load and had to mean the
    opposite for an eject. This is the invariant that stops the next one: an
    expectation is a sentinel that says its own name, or a key the verb's own
    argument list can supply.
    """
    verb = VERBS[verb_name]
    buildable = {argument.key for argument in verb.args} | {key for key, _ in verb.fixed}
    for observe in verb.observes:
        assert isinstance(observe.expect, Expectation) or observe.expect in buildable, (
            f"{verb_name} observes {'.'.join(observe.path)} expecting "
            f"{observe.expect!r}, which its command never carries"
        )


# ----- the mirror must carry what the verbs read ---------------------------

def test_the_mirror_publishes_every_deck_key_a_verb_observes() -> None:
    """An observation of a path the mirror omits can never affirm anything.

    `load` shipped observing `title` alone because `buildUiMirror` did not
    publish `stable_id`, so a load onto an already-loaded deck confirmed off
    the PREVIOUS track's title. Adding the field fixed that instance; this
    reads the live `ui-mirror.ts` so the FIXTURE cannot be the only thing
    holding the two in agreement.
    """
    published = ts_contract.mirror_deck_keys()
    observed = {
        observe.path[2]
        for verb in VERBS.values()
        for observe in verb.observes
        if observe.path[:2] == ("decks", "{deck}")
    }
    assert observed, "no deck observations found; this check would pass vacuously"
    missing = sorted(observed - published)
    assert not missing, (
        f"verbs observe deck keys buildUiMirror does not publish: {missing}. "
        "Publish them in ui-mirror.ts or stop observing them."
    )


def test_a_deck_field_the_mirror_withholds_is_reported_absent() -> None:
    """The negative control for the check above.

    A reader that returned everything, or that silently returned an empty set
    on a parse failure, would pass the containment test no matter what. So
    name a field the deck state really has and the mirror really withholds -
    `is_master` is in `_deckSnapshot` and deliberately not in `buildUiMirror` -
    and require the reader to say it is absent.
    """
    published = ts_contract.mirror_deck_keys()
    assert "stable_id" in published, "the reader cannot see a key that is there"
    assert "is_master" not in published, "the reader cannot say a key is absent"


def test_the_mirror_publishes_the_clock_the_cli_sizes_a_ramp_from() -> None:
    """`master_deck` decides how long a beat-relative ramp may be held open.

    `_ramp` resolves `clock: master` to `before.master_deck` and throws
    `no_master` when there is none (agent-orders.ts), so that field is the only
    thing that tells the CLI which deck's tempo its own order will travel at.
    While the mirror omitted it the CLI sized the deadline off a fixed 60 BPM,
    which the +-100% pitch range makes wrong in the direction that times a
    healthy ramp out (#1739). Read live, so the fixture cannot be what keeps
    the two in agreement.
    """
    published = ts_contract.mirror_top_level_keys()

    assert "master_deck" in published
    assert "master_mode" in published
    assert "master_reason" in published
    assert "decks" in published


def test_a_top_level_field_the_mirror_withholds_is_reported_absent() -> None:
    """The negative control: a reader that returns everything proves nothing.

    `is_master` is per-deck state in `_deckSnapshot` and appears nowhere in
    `buildUiMirror`, at the top level least of all, so the reader must say so
    rather than returning a set that happens to contain what was asked for.
    """
    published = ts_contract.mirror_top_level_keys()

    assert "is_master" not in published
    # Nested keys are not top-level keys: `crossfader` lives inside `mixer`,
    # and a reader that flattened the whole document would swallow the
    # distinction the check above depends on.
    assert "crossfader" not in published


# ----- a scripted command carries the text values a direct one does --------

def test_a_do_command_keeps_a_quoted_text_argument_whole() -> None:
    """``str.split()`` broke a spaced value apart AND kept its quote characters.

    ``hot_cue_save``'s comment is a free-text field, so `do` could not carry a
    value the identical direct invocation accepts: `my` became the comment and
    `comment` was rejected as a stray token. Nesting quotes made it worse, not
    better, because a plain split preserves the quote characters as part of the
    value.
    """
    groups = parse_script(['hot_cue_save 1 A 100 rev-1 "cue for the drop"'])

    assert len(groups) == 1
    assert groups[0].invocations[0].command == {
        "type": "hot_cue_save",
        "deck": 1,
        "slot": "A",
        "in_ms": 100.0,
        "revision": "rev-1",
        "comment": "cue for the drop",
    }


def test_a_quoted_do_command_matches_the_direct_invocation_exactly() -> None:
    """The control: quoting-aware reading must not change what an unspaced
    value builds, only what a spaced one does."""
    scripted = parse_script(["hot_cue_save 1 A 100 rev-1 drop"])[0].invocations[0]
    direct = parse_invocation(["hot_cue_save", "1", "A", "100", "rev-1", "drop"])

    assert scripted.command == direct.command


def test_an_unbalanced_quote_in_a_do_command_is_refused_not_guessed() -> None:
    """The control: a quoting-aware reader must still refuse bad quoting."""
    with pytest.raises(InvocationError):
        parse_script(['hot_cue_save 1 A 100 rev "unterminated'])
