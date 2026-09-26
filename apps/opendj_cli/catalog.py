"""The AGENT-05 verb table: one entry per command on the AGENT-03 bus.

The table is the single source both the argv parser and the ``do`` script
parser build commands from. It is checked against the TypeScript union that
actually validates a command, so it cannot drift silently:
``tests/opendj_cli/test_bus_parity.py`` re-reads
``apps/webui/frontend/src/lib/rb/performance-ipc.svelte.ts`` and fails if a
command type exists there with no verb here, if a verb names a command type
that is not there, or if a verb would emit a field the wire validator rejects
(``_exactKeys`` refuses unknown fields, so an invented key is a runtime red).

Verb ids are seeded from ``QuickDrawActionId``
(``apps/webui/frontend/src/lib/rb/quick-draw-catalog.ts``): every quick-draw
action names the verb that reaches the same command, and the parity test
enforces that too.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from apps.opendj_cli.kinds import (
    DECK_VALUES,
    Arg,
    arg,
    bool_value,
    deck_value,
    enum_value,
    head_delay_ms_value,
    int_enum_value,
    int_value,
    loop_value,
    master_delay_ms_value,
    number_value,
    positive_int_value,
    positive_number_value,
    rescue_decks_value,
    text_value,
    unit_value,
)

STEM_VALUES: tuple[str, ...] = ("vocal", "instrumental", "drums")
BAND_VALUES: tuple[str, ...] = ("low", "mid", "high")
SLOT_VALUES: tuple[str, ...] = tuple("ABCDEFGH")
ASSIGN_VALUES: tuple[str, ...] = ("A", "B", "THRU")
EDGE_VALUES: tuple[str, ...] = ("top", "left", "right", "bottom")
PANEL_VALUES: tuple[str, ...] = ("next", "recommended")
VOTE_VALUES: tuple[str, ...] = ("bad", "good", "great")
SYNC_MODE_VALUES: tuple[str, ...] = ("beat", "bar")
OUTPUT_MODE_VALUES: tuple[str, ...] = ("practice", "two_outputs")
HEADPHONE_ALIGNMENT_MODE_VALUES: tuple[str, ...] = ("headphones_only", "delay_all", "hybrid")
PITCH_RANGE_VALUES: tuple[int, ...] = (8, 16, 100)
KEY_NUDGE_VALUES: tuple[int, ...] = (-1, 1)
QUANTIZE_GRID_VALUES: tuple[int, ...] = (1, 4, 8)
ANALYSIS_SOURCE_FEATURES: tuple[str, ...] = ("beatgrid",)
ANALYSIS_SOURCE_VALUES: tuple[str, ...] = ("rekordbox", "own")

# The command types AGENT-03 will ramp. An ordering that carries a target must
# be one of these, mirroring the guard in the page's own ramp executor
# (apps/webui/frontend/src/lib/rb/agent-orders.ts).
RAMPABLE_TYPES: frozenset[str] = frozenset({"eq", "fader", "trim", "filter", "stem_gain"})

_DECK = arg("deck", "deck", deck_value, "1-4")
_ENABLED = arg("enabled", "bool", bool_value, "true|false")
_SLOT = arg("slot", "enum", enum_value(SLOT_VALUES), "A-H")
_BAND = arg("band", "enum", enum_value(BAND_VALUES), "low|mid|high")
_STEM = arg("stem", "enum", enum_value(STEM_VALUES), "vocal|instrumental|drums")
_UNIT = arg("value", "unit", unit_value, "0..1")


@dataclass(frozen=True)
class Expectation:
    """A mirror expectation that names no command field.

    ``expect=None`` used to carry BOTH of these, which is why ``unload`` was
    unconfirmable: the CLI read "the path must be present and not null" out of
    an absent expectation, while the production mirror answers an unload by
    setting the deck title to null (``_emptyDeckState`` in
    ``apps/webui/frontend/src/lib/player/state.svelte.ts``). One sentinel for
    two opposite outcomes cannot be right for both, so each says its own name.
    """

    label: str

    def __repr__(self) -> str:
        return self.label


PRESENT = Expectation("present and not null")
NULL = Expectation("null")


@dataclass(frozen=True)
class Observe:
    """Where the truth for one verb lives in the UI mirror.

    ``path`` segments are formatted with the built command, so ``{deck}`` and
    ``{band}`` index the right strip. ``expect`` names the command field the
    mirror must agree with, or is one of the :class:`Expectation` sentinels
    (``PRESENT`` / ``NULL``) when the outcome is not a value the command
    carries.
    """

    path: tuple[str, ...]
    expect: str | Expectation
    tolerance: float | None = None


@dataclass(frozen=True)
class Verb:
    """One CLI verb and the bus command it builds."""

    name: str
    command_type: str
    args: tuple[Arg, ...]
    fixed: tuple[tuple[str, Any], ...] = ()
    quick_draws: tuple[str, ...] = ()
    observes: tuple[Observe, ...] = ()
    note: str = ""

    def build(self, values: Mapping[str, Any]) -> dict[str, Any]:
        command: dict[str, Any] = {"type": self.command_type}
        command.update(self.fixed)
        for argument in self.args:
            if argument.key in values:
                command[argument.key] = values[argument.key]
        return command

    def usage(self) -> str:
        return " ".join(argument.signature for argument in self.args)


def _deck_playing(key: str = "playing") -> tuple[Observe, ...]:
    return (Observe(("decks", "{deck}", key), key),)


def _mixer(channel_key: str, expect: str) -> tuple[Observe, ...]:
    return (Observe(("mixer", "channels", "{deck}", channel_key), expect, 1e-6),)


_VERBS: tuple[Verb, ...] = (
    # ----- transport ----------------------------------------------------
    Verb("load", "load", (_DECK, arg("stable_id", "text", text_value)),
         quick_draws=("load",),
         observes=(Observe(("decks", "{deck}", "stable_id"), "stable_id"),)),
    Verb("load_play_intent", "load_play_intent", (
        _DECK,
        arg("generation", "int", positive_int_value),
        arg("desired_play", "bool", bool_value, "true|false"),
    )),
    Verb("unload", "unload", (_DECK,), quick_draws=("unload",),
         observes=(
             Observe(("decks", "{deck}", "title"), NULL),
             Observe(("decks", "{deck}", "stable_id"), NULL),
         )),
    Verb("play", "play", (_DECK,), fixed=(("playing", True),),
         quick_draws=("play.toggle",), observes=_deck_playing()),
    Verb("play_quantized", "play", (_DECK,),
         fixed=(("playing", True), ("quantize", True)),
         observes=(Observe(("decks", "{deck}", "quantized_launch_armed"), PRESENT),)),
    Verb("pause", "play", (_DECK,), fixed=(("playing", False),), observes=_deck_playing()),
    Verb("cue", "cue", (_DECK,), quick_draws=("cue",)),
    Verb("seek", "seek", (_DECK, arg("position_ms", "number", number_value)),
         quick_draws=("seek",)),
    Verb("loop", "loop", (_DECK, arg("loop", "loop", loop_value, "in_ms out_ms", arity=2)),
         note="Nested pair: opendj loop 1 1000 4000."),
    Verb("loop_exit", "loop", (_DECK,), fixed=(("loop", None),), quick_draws=("loop.exit",)),
    Verb("beat_loop", "beat_loop", (
        _DECK,
        arg("beats", "int", int_value),
        arg("start_ms", "number", number_value, optional=True),
    ), quick_draws=("loop.start_4", "loop.start_8", "loop.start_16")),
    Verb("beat_jump", "beat_jump", (_DECK, arg("beats", "int", int_value))),
    Verb("loop_interval_mode", "loop_interval_mode", (_DECK, _ENABLED)),
    Verb("loop_interval_base", "loop_interval_base", (_DECK, arg("base", "number", number_value))),
    Verb("tempo", "tempo", (_DECK, arg("ratio", "number", number_value)),
         quick_draws=("tempo.reset",),
         observes=(Observe(("decks", "{deck}", "pitch"), "ratio", 1e-6),)),
    Verb("pitch_range", "pitch_range", (
        _DECK, arg("range", "int", int_enum_value(PITCH_RANGE_VALUES), "8|16|100"),
    )),
    # ----- deck mode toggles -------------------------------------------
    Verb("quantize", "quantize", (_DECK, _ENABLED), quick_draws=("quantize.toggle",)),
    Verb("quantize_grid", "quantize_grid", (
        _DECK, arg("beats", "int", int_enum_value(QUANTIZE_GRID_VALUES), "1|4|8"),
    ), note="'phase' is on the wire type but not implemented, so it is not offered."),
    Verb("beat_sync", "beat_sync", (_DECK, _ENABLED), quick_draws=("beat_sync.toggle",),
         observes=(Observe(("decks", "{deck}", "sync", "enabled"), "enabled"),)),
    Verb("sync_mode", "sync_mode", (
        _DECK, arg("mode", "enum", enum_value(SYNC_MODE_VALUES), "beat|bar"),
    ), quick_draws=("sync_mode.beat", "sync_mode.bar"),
        observes=(Observe(("decks", "{deck}", "sync", "mode"), "mode"),)),
    Verb("master", "master", (_DECK,), quick_draws=("master",)),
    Verb("master_tempo", "master_tempo", (_DECK, _ENABLED), quick_draws=("master_tempo.toggle",)),
    Verb("stem_mute", "stem_mute", (_DECK, _STEM, arg("muted", "bool", bool_value, "true|false"))),
    Verb("stem_solo", "stem_solo", (_DECK, _STEM, arg("solo", "bool", bool_value, "true|false"))),
    Verb("stem_eq_mode", "stem_eq_mode", (_DECK, _ENABLED),
         observes=(Observe(("mixer", "channels", "{deck}", "stem_eq_mode"), "enabled"),)),
    Verb("stem_gain", "stem_gain", (_DECK, _STEM, _UNIT),
         observes=(Observe(("decks", "{deck}", "stems", "controls", "{stem}", "gain"), "value", 1e-6),)),
    Verb("slip", "slip", (_DECK, _ENABLED), quick_draws=("slip.toggle",)),
    Verb("key_sync", "key_sync", (_DECK, _ENABLED), quick_draws=("key_sync.toggle",)),
    Verb("key_nudge", "key_nudge", (
        _DECK, arg("semitones", "int", int_enum_value(KEY_NUDGE_VALUES), "-1|1"),
    ), quick_draws=("key_nudge.up", "key_nudge.down")),
    # ----- mixer strip --------------------------------------------------
    Verb("trim", "trim", (_DECK, _UNIT), observes=_mixer("trim", "value")),
    Verb("eq", "eq", (_DECK, _BAND, _UNIT),
         observes=(Observe(("mixer", "channels", "{deck}", "eq_{band}"), "value", 1e-6),)),
    Verb("filter", "filter", (_DECK, _UNIT), observes=_mixer("filter", "value")),
    Verb("fader", "fader", (_DECK, _UNIT), observes=_mixer("fader", "value")),
    Verb("assign", "assign", (_DECK, arg("assign", "enum", enum_value(ASSIGN_VALUES), "A|B|THRU")),
         observes=_mixer("assign", "assign")),
    Verb("channel_cue", "channel_cue", (_DECK, _ENABLED),
         observes=_mixer("cue_enabled", "enabled")),
    Verb("crossfader", "crossfader", (_UNIT,),
         observes=(Observe(("mixer", "crossfader"), "value", 1e-6),)),
    Verb("master_volume", "master_volume", (_UNIT,),
         observes=(Observe(("master", "level"), "value", 1e-6),)),
    Verb("master_mute", "master_mute", (arg("muted", "bool", bool_value, "true|false"),),
         observes=(Observe(("master", "muted"), "muted"),)),
    Verb("headphone_mix", "headphone_mix", (_UNIT,)),
    Verb("headphone_level", "headphone_level", (_UNIT,)),
    Verb("head_delay_ms", "head_delay_ms", (
        arg("value", "number", head_delay_ms_value, "0..500"),
    ), note="CUEOUT-03: cue-monitor delay in milliseconds."),
    Verb("headphone_alignment_mode", "headphone_alignment_mode", (
        arg("value", "enum", enum_value(HEADPHONE_ALIGNMENT_MODE_VALUES),
            "headphones_only|delay_all|hybrid"),
    ), note="CUEOUT-14: how a measured cue/master offset is split; re-applies the last calibration."),
    Verb("master_delay_ms", "master_delay_ms", (
        arg("value", "number", master_delay_ms_value, "0..1500"),
    ), note="CUEOUT-14: room (MASTER) delay in milliseconds, the last node before the output."),
    Verb("headphone_calibrate", "headphone_calibrate", (),
         note="CUEOUT-14: headless mic calibration; holds until applied or failed."),
    Verb("headphone_calibrate_abort", "headphone_calibrate_abort", (),
         note="CUEOUT-14: stop an in-flight calibration; decks resume, nothing is applied."),
    # ----- browser, library and headphone outputs -----------------------
    Verb("browser_select_playlist", "browser_select_playlist",
         (arg("playlist_id", "text", text_value),)),
    Verb("headphone_outputs_refresh", "headphone_outputs_refresh", ()),
    # CUEOUT-15: the mini-waveform click, without a pointer. It loads no deck
    # and moves no transport, so it is issuable while all four decks are busy
    # and while one of them is on air. `bpm` is the previewed track's own
    # tempo; supply it and the preview tempo-matches a playing master deck
    # when the setting is on and the match fits the preview pitch range.
    Verb("preview_cue", "preview_cue", (
        arg("stable_id", "text", text_value),
        arg("ratio", "unit", unit_value, "0..1"),
        arg("bpm", "number", positive_number_value, "track BPM", optional=True),
    ), note="CUEOUT-15: play a library track on the cue bus from a point in it."),
    Verb("preview_stop", "preview_stop", (),
         note="CUEOUT-15: stop the preview and release the decoded track; idempotent."),
    Verb("headphone_output_acquire", "headphone_output_acquire", ()),
    Verb("headphone_output_select", "headphone_output_select",
         (arg("device_id", "text", text_value),)),
    Verb("headphone_master_select", "headphone_master_select",
         (arg("device_id", "text", text_value),),
         note="CUEOUT-09: pin AudioContext master sink."),
    Verb("headphone_input_select", "headphone_input_select",
         (arg("device_id", "text", text_value),),
         note="CUEOUT-10: audio input; default is never a headphone/HFP mic."),
    Verb("output_mode", "output_mode", (
        arg("mode", "enum", enum_value(OUTPUT_MODE_VALUES), "practice|two_outputs"),
    ), note="CUEOUT-01: practice blends PFL into the main output; two_outputs is the split."),
    Verb("analysis_source", "analysis_source", (
        arg("feature", "enum", enum_value(ANALYSIS_SOURCE_FEATURES), "beatgrid"),
        arg("source", "enum", enum_value(ANALYSIS_SOURCE_VALUES), "rekordbox|own"),
    ), note="PARITY-02: rbx-vs-own analysis source A/B per feature."),
    Verb("auto_play_two_track", "auto_play_two_track", (),
         note="UI contract only: no automatic second-track selection exists yet."),
    Verb("auto_play_next_arm", "auto_play_next_arm", ()),
    Verb("auto_play_next_cancel", "auto_play_next_cancel", ()),
    Verb("pins_show_other_users", "pins_show_other_users", (),
         note="UI contract only: community pin sync has no channel yet."),
    Verb("library_panels", "library_panels", (
        arg("panel", "enum", enum_value(PANEL_VALUES), "next|recommended"),
        arg("collapsed", "bool", bool_value, "true|false"),
    )),
    Verb("show_stems", "show_stems", (_ENABLED,),
         note="DECKUX-19: per-stem mini-waveforms under the deck wavestack."),
    Verb("feedback_mark", "feedback_mark",
         (arg("vote", "enum", enum_value(VOTE_VALUES), "bad|good|great"),)),
    # RESCUE-01 HTTP parity (not command-bus verbs):
    #   opendj api GET /api/v1/performance/rescue-snapshots/latest
    #   opendj api POST /api/v1/performance/rescue-snapshots --json @snapshot.json
    # AUDIO-DEVICE-01 HTTP parity (issue #923, not command-bus verbs):
    #   opendj audio_output_health
    #   opendj audio_switch_output --confirm
    #   opendj api GET /api/v1/audio/output-health
    #   opendj api POST /api/v1/audio/switch-output
    # FB-20 HTTP parity (issue #4085, not command-bus verbs):
    #   opendj feedback comments summary
    #   opendj api GET /api/v1/feedback/comments/summary
    # ----- safety loop and hot cues -------------------------------------
    Verb("safety_loop_save", "safety_loop_save", (_DECK,)),
    Verb("safety_loop_arm", "safety_loop_arm",
         (_DECK, arg("armed", "bool", bool_value, "true|false"))),
    Verb("safety_loop_clear", "safety_loop_clear", (_DECK,)),
    Verb("hot_cue_save", "hot_cue_save", (
        _DECK,
        _SLOT,
        arg("in_ms", "number", number_value),
        arg("revision", "text", text_value),
        arg("comment", "text", text_value, optional=True),
    )),
    Verb("hot_cue_clear", "hot_cue_clear", (_DECK, _SLOT, arg("revision", "text", text_value))),
    Verb("hot_cue_restore", "hot_cue_restore", (
        _DECK, _SLOT, arg("revision", "text", text_value), arg("reversal_id", "text", text_value),
    )),
    Verb("hot_cue_trigger", "hot_cue_trigger", (_DECK, _SLOT)),
    # ----- technically-working overlay and pairing ----------------------
    Verb("tech_mode_toggle", "tech_mode_toggle", ()),
    Verb("tech_mode_peek", "tech_mode_peek", (arg("peeking", "bool", bool_value, "true|false"),)),
    Verb("tech_mode_opt_reveal", "tech_mode_opt_reveal",
         (arg("revealed", "bool", bool_value, "true|false"),)),
    Verb("tech_mode_eq_raised", "tech_mode_eq_raised",
         (arg("raised", "bool", bool_value, "true|false"),)),
    Verb("tech_mode_edge_hover", "tech_mode_edge_hover", (
        arg("edge", "enum", enum_value(EDGE_VALUES), "top|left|right|bottom"),
        arg("hovered", "bool", bool_value, "true|false"),
    )),
    Verb("pairing_snapshot_open", "pairing_snapshot_open", ()),
    Verb("pairing_snapshot_remove_eq_adjuster", "pairing_snapshot_remove_eq_adjuster",
         (_DECK, _BAND)),
    Verb("pairing_snapshot_save", "pairing_snapshot_save", (
        arg("from_deck", "deck", deck_value, "1-4"),
        arg("to_deck", "deck", deck_value, "1-4"),
    )),
    Verb("playlist_undo", "playlist_undo", (),
         note="Requires a mounted playlist history panel."),
    Verb("playlist_redo", "playlist_redo", (),
         note="Requires a mounted playlist history panel."),
    Verb("rescue_resume", "rescue_resume", (
        arg("decks", "rescue_decks", rescue_decks_value, "1:5000[,3:12000]"),
    ), note="RESCUE-02: schedule every listed deck at position_ms together."),
    Verb("rescue_stop_all", "rescue_stop_all", (),
         note="RESCUE-02 Undo: stop every deck restored by rescue playback together."),
)

VERBS: dict[str, Verb] = {verb.name: verb for verb in _VERBS}
VERB_NAMES: tuple[str, ...] = tuple(verb.name for verb in _VERBS)

# Re-exported so callers that only need "is this a deck?" do not import kinds.
__all__ = [
    "DECK_VALUES",
    "NULL",
    "PRESENT",
    "RAMPABLE_TYPES",
    "VERBS",
    "VERB_NAMES",
    "Expectation",
    "Observe",
    "Verb",
]
