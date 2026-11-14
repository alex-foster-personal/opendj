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

import re
from fractions import Fraction
from pathlib import Path

import pytest

from apps.opendj_cli.catalog import (
    BEAT_FX_EFFECT_VALUES,
    BEAT_FX_TRIPLET_BEAT_VALUES,
    BEAT_JUMP_SIZE_VALUES,
    PAD_MODE_VALUES,
    RAMPABLE_TYPES,
    TRANSITION_PRESET_VALUES,
    VERBS,
    Expectation,
)
from apps.opendj_cli.confirm import checks_for, verdict
from apps.opendj_cli.orders import parse_script
from apps.opendj_cli.verbs import (
    DURATION_ANCHORS,
    InvocationError,
    describe_verbs,
    parse_duration,
    parse_invocation,
)
from tests.opendj_cli import ts_contract

REPO_ROOT = ts_contract.REPO_ROOT

# A token per value kind, tried in order: the first that parses is the sample.
# Derived by asking the verb's own parser rather than by restating the domain,
# so a narrowed domain cannot leave this list quietly wrong.
_SAMPLE_TOKENS = (
    "1", "8", "true", "false", "0.5", "sample", "0 1000", "1:5000",
    "A", "bad", "beat", "low", "next", "top", "vocal",
    "beatgrid", "own", "practice", "hybrid", "pickup", "tri-band", "mono-dev", "mono", "auto", "ai",
    "phase-meter", "smooth", "in", "elastic", "tone", "sections", "space", "stems", "exact", "move", "halo", "half",
    "hot-cue", "chirp",
    "performance", "browser.tree", "300", "open", "second-screen", "bars", "beats_bars", "grid_lines", "off",
    "center",
    "sides",
    "echo", "helix", "spiral", "master", "two-deck", "1-3", "bass-swap-8", "red", "jobs",
    "{}",
    # SAMPLER: a tab ref, an edge and a settings patch.
    "S1-05", "L", "level:80", "vocals",
    # SAMPLER: a classic unit play mode and a master gain in dB.
    "gate", "-6",
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
    if verb_name == "track_info_open":
        return ["sample"]
    tokens: list[str] = []
    # An exactly-one or at-most-one group is sampled by its first member only (SAMPLER: ref, not slot_id too).
    verb = VERBS[verb_name]
    skipped = {key for group in (*verb.exactly_one, *verb.at_most_one) for key in group[1:]}
    for arg in VERBS[verb_name].args:
        if arg.key in skipped:
            continue
        tokens.extend(_sample(verb_name, arg.key).split(" "))
    return tokens


def test_every_command_on_the_bus_has_a_verb() -> None:
    """The acceptance criterion, read straight off the wire contract."""

    declared = set(ts_contract.command_fields())
    named = {verb.command_type for verb in VERBS.values()}
    assert declared - named == set(), "commands with no opendj verb"


@pytest.mark.parametrize("beats", ["0.0625", "0.125", "0.25", "0.5", "4"])
def test_fractional_beat_loop_cli_preserves_the_requested_length(beats: str) -> None:
    assert parse_invocation(["beat_loop", "2", beats, "1000"]).command == {
        "type": "beat_loop", "deck": 2, "beats": float(beats), "start_ms": 1000,
    }


@pytest.mark.requirement("WAVEQS-11736")
@pytest.mark.parametrize("value", ["beats_bars", "bars", "off"])
def test_set_waveform_overlay_grid_lines_builds_the_bus_command(value: str) -> None:
    """[if] grid_lines is set to each allowed value [then] the bus command matches, [else stop]."""
    assert parse_invocation(["set_waveform_overlay", "grid_lines", f"value={value}"]).command == {
        "type": "set_waveform_overlay", "overlay": "grid_lines", "value": value,
    }


@pytest.mark.requirement("WAVEQS-11736")
def test_set_waveform_overlay_rejects_an_unknown_grid_value_and_keeps_the_switch_form() -> None:
    """[if] grid_lines gets a bad value [then] InvocationError and sections still parse, [else stop]."""
    with pytest.raises(InvocationError):
        parse_invocation(["set_waveform_overlay", "grid_lines", "value=dots"])
    assert parse_invocation(["set_waveform_overlay", "sections", "true"]).command == {
        "type": "set_waveform_overlay", "overlay": "sections", "enabled": True,
    }


@pytest.mark.requirement("PLAYLIST-12709")
@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        (["browser_select_playlist", "inbox"], {"type": "browser_select_playlist", "playlist_id": "inbox"}),
        (
            ["browser_select_playlist", "all", "fixture:1"],
            {"type": "browser_select_playlist", "playlist_id": "all", "stable_id": "fixture:1"},
        ),
    ],
)
def test_browser_select_playlist_builds_the_bus_command(
    tokens: list[str], expected: dict[str, str],
) -> None:
    """[if] a valid playlist id is given [then] the bus command carries that id, [else stop]."""
    assert parse_invocation(tokens).command == expected


@pytest.mark.requirement("PLAYLIST-12709")
def test_catalog_observe_and_verdict_against_browser_playlist_mirror() -> None:
    """[if] selection succeeds [then] mirror playlist confirms, [else stop]."""
    verb = VERBS["browser_select_playlist"]
    command = parse_invocation(["browser_select_playlist", "inbox"]).command
    checks = checks_for(verb, command)
    assert len(checks) == 1
    assert checks[0].path == ("browser", "playlist")
    assert checks[0].expected == "inbox"
    assert verdict(checks, {"browser": {"playlist": "inbox"}})[0] == "confirmed"
    assert verdict(checks, {"browser": {"playlist": "other"}})[0] == "unconfirmed"
    assert verdict(checks, {})[0] == "unconfirmed"


def test_roll_cli_parses_fractional_and_whole_lengths() -> None:
    assert parse_invocation(["roll", "1", "1/4"]).command == {
        "type": "roll",
        "deck": 1,
        "beats": 0.25,
    }
    assert parse_invocation(["roll", "1", "2"]).command == {
        "type": "roll",
        "deck": 1,
        "beats": 2,
    }
    assert parse_invocation(["roll_release", "1"]).command == {
        "type": "roll",
        "deck": 1,
        "beats": None,
    }


def test_roll_cli_refuses_an_out_of_set_length() -> None:
    with pytest.raises(InvocationError, match=r"1/32\|1/16"):
        parse_invocation(["roll", "1", "3"])


@pytest.mark.parametrize("verb", ["loop_in", "loop_out", "reloop"])
def test_manual_loop_verbs_build_the_bus_command(verb: str) -> None:
    assert parse_invocation([verb, "2"]).command == {"type": verb, "deck": 2}


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


def test_set_layout_size_refuses_out_of_range_with_the_range() -> None:
    assert parse_invocation(["set_layout_size", "browser.tree", "300"]).command == {
        "type": "set_layout_size",
        "region": "browser.tree",
        "px": 300,
    }
    with pytest.raises(InvocationError, match=r"220\.\.520"):
        parse_invocation(["set_layout_size", "region=browser.tree", "px=9999"])


def test_waveform_seek_builds_the_bus_command_with_snap() -> None:
    assert parse_invocation(["waveform_seek", "2", "12500", "downbeat"]).command == {
        "type": "waveform_seek",
        "deck": 2,
        "position_ms": 12500.0,
        "snap": "downbeat",
    }
    with pytest.raises(InvocationError, match="downbeat\\|beat\\|exact"):
        parse_invocation(["waveform_seek", "1", "0", "instant"])


def test_set_perf_view_and_show_pads_build_the_bus_commands() -> None:
    """[if] the top bar's view icons or PAD button have no verb [then] an agent cannot drive them, [else stop]."""
    assert parse_invocation(["set_perf_view", "phase-scope"]).command == {
        "type": "set_perf_view", "view": "phase-scope",
    }
    assert parse_invocation(["show_pads", "false"]).command == {"type": "show_pads", "enabled": False}
    with pytest.raises(InvocationError):
        parse_invocation(["set_perf_view", "grid"])


def test_set_deck_layout_builds_the_bus_command_and_refuses_bad_values() -> None:
    """[if] the top bar's deck layout toggle has no verb [then] an agent cannot pick two or four decks, [else stop]."""
    assert parse_invocation(["set_deck_layout", "two-deck"]).command == {"type": "set_deck_layout", "layout": "two-deck"}
    assert parse_invocation(["set_deck_layout", "four-deck"]).command == {
        "type": "set_deck_layout",
        "layout": "four-deck",
    }
    with pytest.raises(InvocationError, match="two-deck\\|four-deck"):
        parse_invocation(["set_deck_layout", "three-deck"])


def test_toggle_large_library_builds_the_bus_command() -> None:
    """[if] right Option's large library has no verb [then] an agent cannot toggle it, [else stop]."""
    assert parse_invocation(["toggle_large_library"]).command == {"type": "toggle_large_library"}


@pytest.mark.requirement("LYR-14854")
@pytest.mark.parametrize(("token", "enabled"), [("on", True), ("true", True), ("off", False), ("false", False)])
def test_set_lyrics_global_builds_the_bus_command(token: str, enabled: bool) -> None:
    """[if] opendj set_lyrics_global on|off|true|false does not build the LYR button's typed command [then] parity breaks, [else stop]."""
    assert parse_invocation(["set_lyrics_global", token]).command == {"type": "set_lyrics_global", "enabled": enabled}
    assert parse_invocation(["set_lyrics_global", f"enabled={token}"]).command == {
        "type": "set_lyrics_global", "enabled": enabled,
    }


@pytest.mark.requirement("LYR-14854")
@pytest.mark.parametrize("tokens", [[], ["maybe"], ["on", "extra"], ["on", "deck=1"], ["on", "overlay=lyrics"]])
def test_set_lyrics_global_refuses_missing_invalid_and_extra_arguments(tokens: list[str]) -> None:
    """[if] set_lyrics_global with a missing, non-boolean or extra argument reaches the bus [then] an agent writes a bad pref, [else stop]."""
    with pytest.raises(InvocationError):
        parse_invocation(["set_lyrics_global", *tokens])


@pytest.mark.requirement("LYR-14854")
def test_set_lyrics_global_is_discoverable_in_the_verb_list() -> None:
    """[if] --list-verbs does not name set_lyrics_global with its one enabled field [then] agents cannot find it, [else stop]."""
    rows = [row for row in describe_verbs() if row["command"] == "set_lyrics_global"]
    assert len(rows) == 1
    assert ts_contract.command_fields()["set_lyrics_global"] == {"enabled": False}


def test_pad_mode_builds_the_bus_command_and_tracks_catalog_ids() -> None:
    """[if] pad_mode is not reachable from the CLI or its enum drifts from the catalog [then] agent parity is broken, [else stop]."""
    assert parse_invocation(["pad_mode", "2", "pad-fx-1"]).command == {
        "type": "pad_mode",
        "deck": 2,
        "mode": "pad-fx-1",
    }
    with pytest.raises(InvocationError):
        parse_invocation(["pad_mode", "2", "pads"])
    catalog_path = Path("apps/webui/frontend/src/lib/rb/pad-mode-catalog.ts")
    catalog_ids = re.findall(r"\{ id: '([a-z0-9-]+)'", catalog_path.read_text(encoding="utf-8"))
    assert list(PAD_MODE_VALUES) == catalog_ids


@pytest.mark.requirement("OJ-14010")
def test_transition_preset_builds_the_bus_command_and_tracks_the_ts_preset_ids() -> None:
    """[if] transition_preset misses the CLI, takes an unknown id or drifts from TRANSITION_PRESETS [then] parity breaks, [else stop]."""
    for preset in TRANSITION_PRESET_VALUES:
        assert parse_invocation(["transition_preset", preset]).command == {
            "type": "transition_preset",
            "preset": preset,
        }
    with pytest.raises(InvocationError, match="bass-swap-8"):
        parse_invocation(["transition_preset", "echo-out-8"])
    source = (REPO_ROOT / "apps/webui/frontend/src/lib/rb/transition-presets.ts").read_text(encoding="utf-8")
    ts_ids = re.findall(r"^\t\tid: '([a-z0-9-]+)',$", source, flags=re.MULTILINE)
    assert tuple(ts_ids) == TRANSITION_PRESET_VALUES
    assert ts_contract.command_fields()["transition_preset"] == {"preset": False}


def test_set_deck_number_art_builds_the_bus_command() -> None:
    """[if] set_deck_number_art halo/glow/tinted does not build the typed command [then] agents lose parity with the Appearance setting."""
    for mode in ("halo", "glow", "tinted"):
        assert parse_invocation(["set_deck_number_art", mode]).command == {
            "type": "set_deck_number_art",
            "mode": mode,
        }
    with pytest.raises(InvocationError):
        parse_invocation(["set_deck_number_art", "outline"])


def test_mixer_placement_builds_the_bus_command() -> None:
    """[if] mixer_placement has no verb or refuses center|sides [then] agents lose parity with Settings (#9434)."""
    assert parse_invocation(["mixer_placement", "sides"]).command == {
        "type": "mixer_placement",
        "placement": "sides",
    }
    with pytest.raises(InvocationError, match=r"center\|sides"):
        parse_invocation(["mixer_placement", "left"])


@pytest.mark.requirement("MIDI-13228")
def test_mixer_deck_pair_select_builds_the_bus_command() -> None:
    """[if] an agent sends mixer_deck_pair_select [then] the CLI builds the same bus command MIDI dispatches (#13228), [else stop]."""
    assert parse_invocation(["mixer_deck_pair_select", "2-4"]).command == {
        "type": "mixer_deck_pair_select",
        "pair": "2-4",
    }
    assert parse_invocation(["mixer_deck_pair_select", "1-3", "3"]).command == {
        "type": "mixer_deck_pair_select",
        "pair": "1-3",
        "deck": 3,
    }
    with pytest.raises(InvocationError, match=r"1-3\|2-4"):
        parse_invocation(["mixer_deck_pair_select", "1-2"])


def test_autoplay_verdict_builds_the_bus_command() -> None:
    """[if] the AutoPlay menu's one-tap verdict has no verb [then] an agent cannot give it, [else stop]."""
    assert parse_invocation(["autoplay_verdict", "trk-out", "trk-in", "meh"]).command == {
        "type": "autoplay_verdict", "out": "trk-out", "in": "trk-in", "verdict": "meh",
    }
    with pytest.raises(InvocationError, match="good\\|meh\\|bad"):
        parse_invocation(["autoplay_verdict", "trk-out", "trk-in", "great"])


def test_set_waveform_design_builds_the_bus_command() -> None:
    assert parse_invocation(["set_waveform_design", "tri-band"]).command == {
        "type": "set_waveform_design",
        "design": "tri-band",
    }
    assert parse_invocation(["set_waveform_design", "height=half"]).command == {
        "type": "set_waveform_design",
        "height": "half",
    }
    assert parse_invocation(["set_waveform_design", "tri-band", "half"]).command == {
        "type": "set_waveform_design",
        "design": "tri-band",
        "height": "half",
    }
    assert parse_invocation(["set_waveform_design", "rgb"]).command == {
        "type": "set_waveform_design",
        "design": "rgb",
    }
    with pytest.raises(InvocationError, match="tri-band\\|mono\\|line\\|blocks\\|rgb"):
        parse_invocation(["set_waveform_design", "rainbow"])
    with pytest.raises(InvocationError, match="full\\|half"):
        parse_invocation(["set_waveform_design", "height=quarter"])


def test_set_waveform_overlay_vocal_lane_builds_the_bus_command() -> None:
    """[if] set_waveform_overlay vocal_lane is missing [then] agents cannot hide vocal bars, [else stop]."""
    assert parse_invocation(["set_waveform_overlay", "vocal_lane", "off"]).command == {
        "type": "set_waveform_overlay",
        "overlay": "vocal_lane",
        "enabled": False,
    }
    assert parse_invocation(["set_waveform_overlay", "vocal_lane", "on"]).command == {
        "type": "set_waveform_overlay",
        "overlay": "vocal_lane",
        "enabled": True,
    }
    with pytest.raises(InvocationError):
        parse_invocation(["set_waveform_overlay", "vocal", "off"])


def test_wave_eq_reflect_builds_the_bus_command() -> None:
    assert parse_invocation(["wave_eq_reflect", "false"]).command == {
        "type": "wave_eq_reflect",
        "enabled": False,
    }
    with pytest.raises(InvocationError):
        parse_invocation(["wave_eq_reflect", "maybe"])


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


# Codex P2 on PR #6235: the loop-source menu engages a stored loop exactly
# (quantize false); an agent must be able to send the same command.
def test_a_loop_can_be_engaged_exactly_as_the_stored_loop_menu_does() -> None:
    assert parse_invocation(["loop", "1", "1000", "4000", "false"]).command == {
        "type": "loop", "deck": 1, "loop": {"in_ms": 1000.0, "out_ms": 4000.0}, "quantize": False,
    }
    # Control: omitted, the command is unchanged and the dispatcher's default applies.
    assert "quantize" not in parse_invocation(["loop", "1", "1000", "4000"]).command
    # The dispatcher accepts only false (omission is the default), so true is
    # refused here rather than sent and rejected remotely.
    with pytest.raises(InvocationError, match="may only be false"):
        parse_invocation(["loop", "1", "1000", "4000", "true"])


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
    """agent-orders.ts refuses a ramp over anything but these (AGENT-24 added
    crossfader and tempo). Read off the page's own RAMP_TYPES set, so the two
    lists cannot drift."""
    source = (ts_contract.REPO_ROOT / "apps/webui/frontend/src/lib/rb/agent-orders.ts").read_text()
    match = re.search(r"const RAMP_TYPES = new Set\(\[([^\]]*)\]\)", source)
    assert match is not None, "agent-orders.ts no longer declares RAMP_TYPES"
    page_types = set(re.findall(r"'([a-z_]+)'", match.group(1)))
    assert page_types == RAMPABLE_TYPES
    assert {"eq", "fader", "trim", "filter", "stem_gain", "stem_fx_send", "crossfader", "tempo"} == RAMPABLE_TYPES


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


def test_the_mirror_publishes_every_browser_key_a_verb_observes() -> None:
    """An observation of a browser path the mirror omits can never affirm anything."""
    published = ts_contract.mirror_browser_keys()
    observed = {
        observe.path[1]
        for verb in VERBS.values()
        for observe in verb.observes
        if len(observe.path) >= 2 and observe.path[0] == "browser"
    }
    assert observed, "no browser observations found; this check would pass vacuously"
    missing = sorted(observed - published)
    assert not missing, (
        f"verbs observe browser keys buildUiMirror does not publish: {missing}. "
        "Publish them in ui-mirror.ts or stop observing them."
    )


def test_a_browser_field_the_mirror_withholds_is_reported_absent() -> None:
    """The negative control for the browser mirror reader."""
    published = ts_contract.mirror_browser_keys()
    assert "playlist" in published, "the reader cannot see a key that is there"
    assert "active_playlist" not in published, "the reader cannot say a key is absent"


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


def test_the_mirror_publishes_every_top_level_key_a_verb_observes() -> None:
    """A one-segment observation reads the mirror's top level, so it must be there.

    `deck_autocue` shipped with no observation at all, because neither the
    performance snapshot nor the mirror carried AU, so an agent could switch it
    but never read it back. Read live, so the fixture cannot be what keeps the
    two in agreement; the control below proves the reader can say absent.
    """
    published = ts_contract.mirror_top_level_keys()
    observed = {
        observe.path[0]
        for verb in VERBS.values()
        for observe in verb.observes
        if len(observe.path) == 1
    }
    assert {"autoplay_enabled", "deck_autocue"} <= observed, "the verbs this guards no longer observe"
    missing = sorted(observed - published)
    assert not missing, (
        f"verbs observe top-level keys buildUiMirror does not publish: {missing}. "
        "Publish them in ui-mirror.ts or stop observing them."
    )


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


def test_grid_edit_builds_the_nested_edit_the_dispatcher_validates() -> None:
    """GRIDEDIT-6235d: a word resets that part, a number nudges (ms, then whole beats)."""

    assert parse_invocation(["grid_edit", "1", "-2.5:1"]).command == {
        "type": "grid_edit", "deck": 1, "edit": {"kind": "nudge", "offset_ms": -2.5, "beats": 1},
    }
    assert parse_invocation(["grid_edit", "2", "4"]).command["edit"] == {
        "kind": "nudge", "offset_ms": 4.0, "beats": 0,
    }
    assert parse_invocation(["grid_edit", "2", "shift"]).command["edit"] == {
        "kind": "reset", "part": "shift",
    }
    # GRIDEDIT-10215: the deck's Cmd+Z sends the same command.
    assert parse_invocation(["grid_edit", "1", "undo"]).command["edit"] == {"kind": "undo"}
    # The same bounds parseGridEditIntent enforces, refused before dispatch.
    for token in ("501", "2:4", "2:1.5", "nan"):
        with pytest.raises(InvocationError):
            parse_invocation(["grid_edit", "1", token])


def test_quantize_grid_accepts_phase_as_the_page_does() -> None:
    """DECKCTL-6235c: an agent selects phrase quantize from the CLI too (Codex P2, PR #6235)."""

    assert parse_invocation(["quantize_grid", "1", "phase"]).command == {
        "type": "quantize_grid", "deck": 1, "beats": "phase",
    }
    # Control: whole-beat grids still parse as ints, and other values are refused.
    assert parse_invocation(["quantize_grid", "1", "4"]).command["beats"] == 4
    for token in ("2", "bar", "Phase"):
        with pytest.raises(InvocationError):
            parse_invocation(["quantize_grid", "1", token])


def test_beat_jump_size_builds_the_bus_command_for_every_rekordbox_size() -> None:
    """DECKUX-10470: if beat_jump_size is not reachable from the CLI then agent parity is broken."""
    assert parse_invocation(["beat_jump_size", "1", "0.5"]).command == {
        "type": "beat_jump_size",
        "deck": 1,
        "beats": 0.5,
    }
    assert parse_invocation(["beat_jump_size", "2", "64"]).command["beats"] == 64
    with pytest.raises(InvocationError, match=r"0\.5\|1\|2\|4"):
        parse_invocation(["beat_jump_size", "1", "3"])


@pytest.mark.requirement("DECKUX-14853")
def test_beat_jump_size_catalog_tracks_the_typed_command() -> None:
    """[if] the CLI size list, its usage hint or its fields drift from BEAT_JUMP_SIZES and the TS union [then] parity breaks, [else stop]."""
    source = (REPO_ROOT / "apps/webui/frontend/src/lib/rb/beat-jump-size.svelte.ts").read_text(encoding="utf-8")
    match = re.search(r"^export const BEAT_JUMP_SIZES = \[([^\]]+)\] as const;$", source, flags=re.MULTILINE)
    assert match is not None, "if BEAT_JUMP_SIZES is not found in beat-jump-size.svelte.ts then this guard is blind"
    assert tuple(float(token) for token in match.group(1).split(",")) == BEAT_JUMP_SIZE_VALUES
    hint = "|".join(f"{size:g}" for size in BEAT_JUMP_SIZE_VALUES)
    assert VERBS["beat_jump_size"].usage() == f"<deck:1-4> <beats:{hint}>"
    assert ts_contract.command_fields()["beat_jump_size"] == {"deck": False, "beats": False}


def test_color_fx_all_reaches_every_channel_as_one_command() -> None:
    """COLORFX-13: deck=all is color_fx-only and reaches the bus command agents send."""
    assert parse_invocation(["color_fx", "all", "space"]).command == {
        "type": "color_fx",
        "deck": "all",
        "effect": "space",
    }
    assert parse_invocation(["color_fx", "1", "space"]).command["deck"] == 1
    with pytest.raises(InvocationError):
        parse_invocation(["fader", "all", "0.5"])


@pytest.mark.requirement("TRANSAI-10667")
def test_propose_transition_parity_bytes_are_stable_across_calls() -> None:
    """[if] the same pair is proposed twice [then] confidence and rejected parity bytes match, [else stop]."""
    from apps.autodj.http import propose_parity_bytes, propose_transition
    from tests.autodj.fixtures import profile

    out_profile, in_profile = profile("parity-out"), profile("parity-in", key="9A")
    first = propose_transition(out_profile, in_profile, out_deck=1, in_deck=2, top=2)
    second = propose_transition(out_profile, in_profile, out_deck=1, in_deck=2, top=2)
    for document in (first, second):
        document.pop("_valid_proposal_scores", None)
    assert propose_parity_bytes(first) == propose_parity_bytes(second)
    assert first["proposals"][0]["confidence"] == second["proposals"][0]["confidence"]


def test_beat_jump_accepts_a_half_beat_and_refuses_other_fractions() -> None:
    """DECKUX-10470: if beat_jump refuses -0.5 or accepts 1.5 then the half-beat jump is broken."""
    assert parse_invocation(["beat_jump", "1", "-0.5"]).command["beats"] == -0.5
    assert parse_invocation(["beat_jump", "1", "8"]).command["beats"] == 8
    with pytest.raises(InvocationError, match="non-zero whole number or"):
        parse_invocation(["beat_jump", "1", "1.5"])


@pytest.mark.requirement("FXECHO-10722")
def test_beat_fx_effect_values_match_ts_catalog() -> None:
    """[if] BEAT_FX_EFFECT_VALUES drifts from the beat FX registry names [then] agent parity breaks, [else stop]."""

    effects_dir = REPO_ROOT / "apps/webui/frontend/src/lib/rb/fx/beat-fx-effects"
    catalog_path = REPO_ROOT / "apps/webui/frontend/src/lib/rb/fx/time-fx-catalog.ts"
    registered: list[str] = []
    for stem in re.findall(r"^import '\./([\w-]+)';$", (effects_dir / "index.ts").read_text(encoding="utf-8"), re.M):
        # time-effects.ts registers every time catalog row; any other effect file names itself.
        source = catalog_path if stem == "time-effects" else effects_dir / f"{stem}.ts"
        registered += re.findall(r"^\s*name: '(\w+)',$", source.read_text(encoding="utf-8"), re.M)
    assert registered, "no beat FX effects found in the registry"
    assert tuple(registered) == BEAT_FX_EFFECT_VALUES


def test_beat_fx_cli_builds_the_bus_command() -> None:
    assert parse_invocation(["beat_fx", "target=master", "effect=helix", "enabled=true"]).command == {
        "type": "beat_fx",
        "target": "master",
        "effect": "helix",
        "enabled": True,
    }


# --- WAVEQS-8348e (issue #12521): the popover switches that gained the overlay verb ---


@pytest.mark.requirement("WAVEQS-8348e")
@pytest.mark.parametrize("overlay", ["sections", "lyrics", "lyrics_scrub", "jog_radial"])
@pytest.mark.parametrize("enabled", [True, False])
def test_set_waveform_overlay_cli_builds_each_popover_switch(overlay: str, enabled: bool) -> None:
    """[if] the CLI sets a popover overlay on or off [then] the bus command carries it, [else stop]."""
    word = "true" if enabled else "false"
    assert parse_invocation(["set_waveform_overlay", overlay, word]).command == {
        "type": "set_waveform_overlay", "overlay": overlay, "enabled": enabled,
    }


@pytest.mark.requirement("WAVEQS-8348e")
@pytest.mark.parametrize("overlay", ["beats", "lyric_scrub", "scrub"])
def test_set_waveform_overlay_cli_refuses_an_unknown_overlay(overlay: str) -> None:
    """[if] the CLI gets an overlay name the bus does not know [then] InvocationError, [else stop]."""
    with pytest.raises(InvocationError):
        parse_invocation(["set_waveform_overlay", overlay, "true"])


@pytest.mark.requirement("TRANSAI-14006")
def test_stem_gain_accepts_the_other_part() -> None:
    """[if] opendj stem_gain --deck 1 --stem other --value 0 parses [then] it builds the stem_gain other command, [else stop]."""
    parsed = parse_invocation(["stem_gain", "--deck", "1", "--stem", "other", "--value", "0"])
    assert parsed.command == {"type": "stem_gain", "deck": 1, "stem": "other", "value": 0.0}
    assert parsed.verb.observes[0].path == ("decks", "{deck}", "stems", "controls", "{stem}", "gain")


@pytest.mark.requirement("BEATFX-14616")
def test_beat_fx_cli_sends_time_ms_and_notes_range_and_exclusivity() -> None:
    """[if] opendj beat_fx time_ms=375 does not reach the bus as time_ms, or the note lacks 1..4000 and beats [then] CLI parity breaks, [else stop]."""
    assert parse_invocation(["beat_fx", "effect=echo", "time_ms=375"]).command == {
        "type": "beat_fx",
        "effect": "echo",
        "time_ms": 375,
    }
    note = VERBS["beat_fx"].note
    assert "1..4000 ms" in note and "time_ms and beats in one command are refused" in note


@pytest.mark.requirement("BEATFX-14715")
def test_beat_fx_cli_sends_triplet_beats_and_matches_the_ts_triplet_set() -> None:
    """[if] opendj beat_fx triplet=true beats=1/3 misses the bus or the triplet set drifts from mod-fx-lfo [then] CLI parity breaks, [else stop]."""
    assert parse_invocation(["beat_fx", "triplet=true", "beats=1/3"]).command == {
        "type": "beat_fx",
        "triplet": True,
        "beats": 1 / 3,
    }
    lfo_source = (REPO_ROOT / "apps/webui/frontend/src/lib/rb/mod-fx-lfo.ts").read_text(encoding="utf-8")
    ts_list = re.search(r"TRIPLET_CYCLE_BEATS = \[([^\]]+)\]", lfo_source)
    assert ts_list is not None, "TRIPLET_CYCLE_BEATS not found in mod-fx-lfo.ts"
    ts_values = tuple(float(Fraction(part.replace(" ", ""))) for part in ts_list.group(1).split(","))
    assert ts_values == BEAT_FX_TRIPLET_BEAT_VALUES
    assert "triplet=true" in VERBS["beat_fx"].note
    with pytest.raises(ValueError, match="beats"):
        parse_invocation(["beat_fx", "beats=3"])


@pytest.mark.requirement("BEATFX-14526")
def test_beat_fx_cli_takes_crush_with_level_and_no_beats() -> None:
    """[if] opendj beat_fx effect=crush level=0.4 is refused or grows a beats field [then] agents cannot crush, [else stop]."""
    assert parse_invocation(["beat_fx", "effect=crush", "level=0.4", "enabled=true"]).command == {
        "type": "beat_fx",
        "effect": "crush",
        "level": 0.4,
        "enabled": True,
    }


@pytest.mark.requirement("LIBUX-02")
@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        (["library_panels_toggle"], {"type": "library_panels_toggle"}),
        (["library_panels_toggle", "true"], {"type": "library_panels_toggle", "collapsed": True}),
        (["library_panels_toggle", "false"], {"type": "library_panels_toggle", "collapsed": False}),
    ],
)
def test_library_panels_toggle_cli_builds_the_same_bus_command_as_the_chevron(
    tokens: list[str], expected: dict[str, object],
) -> None:
    """[if] the CLI verb runs [then] it emits the chevron's library_panels_toggle command, [else stop]."""
    command = parse_invocation(tokens).command
    assert command == expected
    wire = ts_contract.command_fields()["library_panels_toggle"]
    assert wire == {"collapsed": True}


@pytest.mark.requirement("LIBUX-02")
def test_library_panels_toggle_cli_rejects_a_non_boolean_and_stays_apart_from_legacy_panels() -> None:
    """[if] collapsed is not a boolean [then] InvocationError, and the legacy verb keeps its own type, [else stop]."""
    with pytest.raises(InvocationError):
        parse_invocation(["library_panels_toggle", "maybe"])
    assert parse_invocation(["library_panels", "next", "true"]).command == {
        "type": "library_panels", "panel": "next", "collapsed": True,
    }


# --- DECKUX-6794 (issue #12534): snapped grid_jump shares one command contract across UI, CLI and Rust ---


@pytest.mark.requirement("DECKUX-6794")
@pytest.mark.parametrize(("deck", "bars"), [("1", 4), ("2", -4), ("1", 8), ("2", -8)])
def test_grid_jump_cli_builds_the_command_the_keys_send(deck: str, bars: int) -> None:
    """[if] `opendj grid_jump` cannot send the keys' 4 or 8 bar step [then] parity breaks, [else stop]."""
    assert parse_invocation(["grid_jump", deck, str(bars)]).command == {
        "type": "grid_jump",
        "deck": int(deck),
        "bars": bars,
    }


@pytest.mark.requirement("DECKUX-6794")
def test_grid_jump_cli_matches_the_wire_contract_and_refuses_a_fractional_step() -> None:
    """[if] the CLI drifts from grid_jump {deck, bars} or takes 2.5 bars [then] broken, [else stop]."""
    assert ts_contract.command_fields()["grid_jump"] == {"deck": False, "bars": False}
    with pytest.raises(InvocationError, match="bars must be an integer"):
        parse_invocation(["grid_jump", "1", "2.5"])

