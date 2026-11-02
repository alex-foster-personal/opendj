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
    auto_gain_offset_value,
    bool_value,
    deck_value,
    enum_value,
    false_only_value,
    grid_edit_value,
    head_delay_ms_value,
    int_enum_value,
    int_value,
    loop_value,
    master_delay_ms_value,
    number_value,
    positive_int_value,
    positive_number_value,
    quantize_grid_value,
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
MIDI_TAKEOVER_MODE_VALUES: tuple[str, ...] = ("pickup", "jump")
PITCH_RANGE_VALUES: tuple[int, ...] = (8, 16, 100)
KEY_NUDGE_VALUES: tuple[int, ...] = (-1, 1)
QUANTIZE_GRID_VALUES: tuple[int, ...] = (1, 4, 8)
ANALYSIS_SOURCE_FEATURES: tuple[str, ...] = ("beatgrid",)
ANALYSIS_SOURCE_VALUES: tuple[str, ...] = ("rekordbox", "own")
WAVEFORM_SEEK_SNAP_VALUES: tuple[str, ...] = ("downbeat", "beat", "exact")
WAVEFORM_DESIGN_VALUES: tuple[str, ...] = ("auto", "tri-band", "mono", "line", "blocks")
# TOPVIEW-01..03: mirrors PERF_VIEWS in apps/webui/frontend/src/lib/rb/topbar-view-prefs.ts.
TRANSITION_INTENT_VALUES: tuple[str, ...] = ("smooth", "energy_up", "wind_down", "showy", "vocals_clean")
PERF_VIEW_VALUES: tuple[str, ...] = ("waveform", "list", "phase-scope", "phase-meter")
UI_SKIN_VALUES: tuple[str, ...] = ("default", "mono-dev", "light")
WAVE_PALETTE_VALUES: tuple[str, ...] = ("auto", "rekordbox", "legacy", "mono")
WAVE_SPLIT_MASTER_VALUES: tuple[str, ...] = ("auto", "on", "off")

# The command types AGENT-03 will ramp. An ordering that carries a target must
# be one of these, mirroring the guard in the page's own ramp executor
# (apps/webui/frontend/src/lib/rb/agent-orders.ts). AGENT-24 added crossfader
# and tempo (the canon AutoDJ doc's first gap); tempo travels as a ratio.
RAMPABLE_TYPES: frozenset[str] = frozenset(
    {"eq", "fader", "trim", "filter", "stem_gain", "crossfader", "tempo"}
)

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
    Verb("seek", "seek", (
        _DECK,
        arg("position_ms", "number", number_value),
        arg("exact", "bool", bool_value, "true|false", optional=True),
    ), quick_draws=("seek",)),
    Verb("waveform_seek", "waveform_seek", (
        _DECK,
        arg("position_ms", "number", number_value),
        arg("snap", "enum", enum_value(WAVEFORM_SEEK_SNAP_VALUES), "downbeat|beat|exact"),
    )),
    Verb("loop", "loop", (
        _DECK,
        arg("loop", "loop", loop_value, "in_ms out_ms", arity=2),
        arg("quantize", "bool", false_only_value, "false", optional=True),
    ), note="Nested pair: opendj loop 1 1000 4000. DECKCTL-6235d: a trailing false engages the range "
            "exactly as given (a stored loop), even with Quantize on; omit it for the default."),
    Verb("loop_exit", "loop", (_DECK,), fixed=(("loop", None),), quick_draws=("loop.exit",)),
    Verb("beat_loop", "beat_loop", (
        _DECK,
        arg("beats", "number", number_value),
        arg("start_ms", "number", number_value, optional=True),
    ), quick_draws=("loop.start_4", "loop.start_8", "loop.start_16")),
    Verb("beat_jump", "beat_jump", (_DECK, arg("beats", "int", int_value))),
    Verb("grid_jump", "grid_jump", (_DECK, arg("bars", "int", int_value)),
         note="DECKUX-6794: snapped jump on the fixed bar grid; -4 back, 4 forward, 8 with Shift."),
    Verb("loop_interval_mode", "loop_interval_mode", (_DECK, _ENABLED)),
    Verb("loop_interval_base", "loop_interval_base", (_DECK, arg("base", "number", number_value))),
    Verb("tempo", "tempo", (_DECK, arg("ratio", "number", number_value)),
         quick_draws=("tempo.reset",),
         observes=(Observe(("decks", "{deck}", "pitch"), "ratio", 1e-6),)),
    Verb("pitch_range", "pitch_range", (
        _DECK, arg("range", "int", int_enum_value(PITCH_RANGE_VALUES), "8|16|100"),
    )),
    # ----- deck mode toggles -------------------------------------------
    Verb("quantize", "quantize", (
        _DECK, _ENABLED, arg("by_user", "bool", bool_value, "true|false", optional=True),
    ), quick_draws=("quantize.toggle",),
         note="DECKUX-39: an off is refused (422) unless by_user is true (a person asked); "
              "a user off holds until the deck's next load."),
    Verb("quantize_grid", "quantize_grid", (
        _DECK, arg("beats", "quantize_grid", quantize_grid_value(QUANTIZE_GRID_VALUES), "1|4|8|phase"),
    ), note="DECKCTL-6235c: phase snaps to phrase starts (the bar, 4 beats, on a track with no phrases)."),
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
    Verb("stem_load", "stem_load", (_DECK,),
         note="STEM-46/47: get the deck's stems now (retry a failed load, start a held one)."),
    Verb("stem_separate", "stem_separate", (_DECK, arg("stable_id", "text", text_value, optional=True)),
         note="STEM-77: enqueue separation of the track on a NO STEMS deck; "
              "refused while a job for it is queued or running, or when the deck no longer "
              "holds stable_id (omitted: the track it holds when the command arrives). "
              "stem_load lands the result."),
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
    Verb("auto_gain_offset", "auto_gain_offset", (
        _DECK, arg("offset_db", "number", auto_gain_offset_value, "-12..12|clear"),
        arg("stable_id", "text", text_value),
    ), note="AUTOGAIN-04: the loaded track's own auto gain dB; clear uses the analysed value. "
            "stable_id names the track meant; a deck holding another one refuses the write."),
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
    Verb("midi_takeover_mode", "midi_takeover_mode", (
        arg("mode", "enum", enum_value(MIDI_TAKEOVER_MODE_VALUES), "pickup|jump"),
    ), note="IOPIN-06: pickup holds a MIDI dial until the hardware crosses the software value."),
    Verb("analysis_source", "analysis_source", (
        arg("feature", "enum", enum_value(ANALYSIS_SOURCE_FEATURES), "beatgrid"),
        arg("source", "enum", enum_value(ANALYSIS_SOURCE_VALUES), "rekordbox|own"),
    ), note="PARITY-02: rbx-vs-own analysis source A/B per feature."),
    Verb("auto_play_two_track", "auto_play_two_track", (_ENABLED,),
         note="TOPVIEW-06: two-track AutoPlay keeps the set on decks 1 and 2."),
    Verb("set_perf_view", "set_perf_view", (
        arg("view", "enum", enum_value(PERF_VIEW_VALUES), "waveform|list|phase-scope|phase-meter"),
    ), note="TOPVIEW-01..03: the top bar's view icons; what the wavestack area shows."),
    Verb("show_pads", "show_pads", (_ENABLED,),
         note="TOPVIEW-04: the top bar's PAD button; show or hide the performance-pad rows."),
    Verb("key_nudge_step", "key_nudge_step", (
        arg("step", "enum", enum_value(("semitone", "tone", "scale", "camelot")), "semitone|tone|scale|camelot"),
    ), note="DECKGAP-7691b: the key nudge arrows' step size, shared by every deck (camelot = one fifth)."),
    Verb("track_load_reset", "track_load_reset", tuple(
        arg(item, "bool", bool_value, "true|false") for item in ("level", "stems", "eq", "trim", "filter")
    ), note="DECKGAP-7691: which mixer values return to neutral when a track loads (all five, every deck)."),
    Verb("auto_play_next_arm", "auto_play_next_arm", (
        arg("intent", "enum", enum_value(TRANSITION_INTENT_VALUES), "smooth|energy_up|wind_down|showy|vocals_clean", optional=True),
        arg("out_deck", "deck", deck_value, "1-4", optional=True),
        arg("in_deck", "deck", deck_value, "1-4", optional=True),
        arg("from_ms", "number", number_value, "ms on the outgoing track", optional=True),
    ), note="AGENT-48: plan the next transition from the live position; the fixed loop runs when no plan is eligible. "
            "AGENT-50: out_deck, in_deck and from_ms plan out of that deck around that point (no fallback)."),
    Verb("auto_play_next_cancel", "auto_play_next_cancel", ()),
    Verb("auto_play_decide_now", "auto_play_decide_now", (),
         note="AUTOPLAN-06: AutoPlay decides now rather than at its target; read autoplay_decision on the mirror."),
    Verb("auto_play_early", "auto_play_early", (
        arg("enabled", "bool", bool_value, "true|false", optional=True),
        arg("decide_after_s", "number", number_value, "seconds on the master", optional=True),
    ), note="AUTOPLAN-01: AutoPlay's early planning and its soft target; the AutoPlay menu's twin."),
    Verb("pins_show_other_users", "pins_show_other_users", (),
         note="UI contract only: community pin sync has no channel yet."),
    Verb("library_panels", "library_panels", (
        arg("panel", "enum", enum_value(PANEL_VALUES), "next|recommended"),
        arg("collapsed", "bool", bool_value, "true|false"),
    )),
    Verb("deck_autocue", "deck_autocue", (_ENABLED,),
         observes=(Observe(("deck_autocue",), "enabled"),),
         note="DECKCTL-6235: the AU switch; a fresh load parks at its first memory cue, else its first beat."),
    Verb("show_stems", "show_stems", (_ENABLED,),
         note="DECKUX-19: per-stem mini-waveforms under the deck wavestack."),
    Verb("autoplay", "autoplay", (
        _ENABLED, arg("by_user", "bool", bool_value, "true|false", optional=True),
    ),
         observes=(Observe(("autoplay_enabled",), "enabled"),),
         note="AGENT-20: the AutoPlay switch; the mirror's autoplay_armed says if it will hand off. "
              "PLAY-18: off is refused unless by_user is true (a person asked)."),
    Verb("set_waveform_design", "set_waveform_design", (
        arg("design", "enum", enum_value(WAVEFORM_DESIGN_VALUES), "auto|tri-band|mono|line|blocks"),
    )),
    Verb("set_skin", "set_skin", (
        arg("ui_skin", "enum", enum_value(UI_SKIN_VALUES), "default|mono-dev|light"),
        arg("wave_palette", "enum", enum_value(WAVE_PALETTE_VALUES), "auto|rekordbox|legacy|mono"),
        arg("wave_split_master", "enum", enum_value(WAVE_SPLIT_MASTER_VALUES), "auto|on|off"),
    ), note="Skin preview: chrome skin, waveform palette and split master waveform in one call. "
            "wave_split_master is opt-in: auto = off on every skin (mirrored rows), on = split."),
    Verb("grid_edit", "grid_edit", (
        _DECK, arg("edit", "grid_edit", grid_edit_value, "adjust|shift|<offset_ms>[:<beats>]"),
    ), note="GRIDEDIT-6235d: nudge the loaded track's stored grid (ms, plus whole beats) or reset "
            "the adjust or shift part; the same queue the deck's grid buttons use."),
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
    # ----- top bar Track information (i) overlay (TOPVIEW-05) -----------
    Verb("track_info_toggle", "track_info_toggle", ()),
    Verb("track_info_close", "track_info_close", ()),
    Verb("pairing_snapshot_open", "pairing_snapshot_open", ()),
    Verb("pairing_snapshot_remove_eq_adjuster", "pairing_snapshot_remove_eq_adjuster",
         (_DECK, _BAND)),
    Verb("pairing_snapshot_save", "pairing_snapshot_save", (
        arg("from_deck", "deck", deck_value, "1-4"),
        arg("to_deck", "deck", deck_value, "1-4"),
    )),
    Verb("cmdk_open", "cmdk_open", (arg("query", "text", text_value, optional=True),),
         note="Requires the Cmd K v2 bar (flag cmdk.v2) on /performance."),
    Verb("cmdk_close", "cmdk_close", (),
         note="Requires the Cmd K v2 bar (flag cmdk.v2) on /performance."),
    Verb("cmdk_pick", "cmdk_pick", (
        arg("index", "int", int_value, "row (0 = first)"),
        arg("id", "text", text_value,
            "row id exactly as query().cmdk reports it "
            "(a pair row is pair:<stable_id>:loose or pair:<stable_id>:aligned:<basis>)",
            optional=True),
        arg("deck", "deck", deck_value, "1-4", optional=True),
        arg("target", "deck", deck_value, "the target query().cmdk reported; refused if it moved", optional=True),
        arg("confirm", "bool", bool_value, "true|false", optional=True),
        arg("vocals_only", "bool", bool_value,
            "true|false: Shift+Enter, the track loads and its vocals are soloed once the stems are ready",
            optional=True),
    ), note="CMDK-19: pick row `index` of the open Cmd K bar as Enter does (a track loads to `deck`, "
            "an aligned pair at its captured offset with the fader at 0); `confirm` is Cmd+Enter "
            "over a playing deck; `vocals_only` is Shift+Enter. Name optional fields to skip earlier ones, "
            "as in `deck=2 confirm=true`. Requires the Cmd K v2 bar on /performance, open."),
    Verb("cmdk_mode", "cmdk_mode", (
        arg("mode", "enum", enum_value(("plain", "ai")), "plain|ai"),
    ), note="CMDK-20: switch the open Cmd K bar between plain and AI search, as Tab does. "
            "Requires the Cmd K v2 bar on /performance, open, with flag cmdk.v2.ai on."),
    Verb("playlist_undo", "playlist_undo", (),
         note="Requires a mounted playlist history panel."),
    Verb("playlist_redo", "playlist_redo", (),
         note="Requires a mounted playlist history panel."),
    Verb("rescue_resume", "rescue_resume", (
        arg("decks", "rescue_decks", rescue_decks_value, "1:5000[,3:12000]"),
        arg("master_deck", "deck", deck_value, "1-4"),
    ), note="RESCUE-02: schedule every listed deck at position_ms together; RESCUE-06: master_deck "
            "(one of the listed decks) is master once they start."),
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
