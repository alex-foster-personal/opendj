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
    LAYOUT_REGION_BOUNDS,
    Arg,
    arg,
    auto_gain_offset_value,
    beat_jump_beats_value,
    beat_jump_size_value,
    bool_value,
    deck_or_all_value,
    deck_or_master_value,
    deck_value,
    dial_value,
    enum_value,
    false_only_value,
    fraction_enum_value,
    grid_edit_value,
    head_delay_ms_value,
    int_enum_value,
    int_value,
    json_object_value,
    layout_px_value,
    loop_value,
    master_delay_ms_value,
    number_enum_value,
    number_value,
    positive_int_value,
    positive_number_value,
    quantize_grid_value,
    related_bpm_percent_value,
    rescue_decks_value,
    reset_parts_value,
    roll_beats_value,
    text_value,
    unit_value,
)

STEM_VALUES: tuple[str, ...] = ("vocal", "instrumental", "drums", "bass", "other")
BAND_VALUES: tuple[str, ...] = ("low", "mid", "high")
SLOT_VALUES: tuple[str, ...] = tuple("ABCDEFGH")
ASSIGN_VALUES: tuple[str, ...] = ("A", "B", "THRU")
EDGE_VALUES: tuple[str, ...] = ("top", "left", "right", "bottom")
# PERFDRAWER-12580: top-bar drawers drawer_open / drawer_close reach (the JOBS drawer).
DRAWER_VALUES: tuple[str, ...] = ("jobs",)
PANEL_VALUES: tuple[str, ...] = ("next", "recommended")
VOTE_VALUES: tuple[str, ...] = ("bad", "good", "great")
SYNC_MODE_VALUES: tuple[str, ...] = ("beat", "bar")
DECK_MOVE_MODE_VALUES: tuple[str, ...] = ("move", "copy")
OUTPUT_MODE_VALUES: tuple[str, ...] = ("practice", "two_outputs", "split_cable")
HEADPHONE_ALIGNMENT_MODE_VALUES: tuple[str, ...] = ("headphones_only", "delay_all", "hybrid")
CALIBRATION_PROBE_VALUES: tuple[str, ...] = ("chirp", "click_train")
BOOTH_SIGNAL_MODE_VALUES: tuple[str, ...] = ("off", "follows_master", "independent")
MIDI_TAKEOVER_MODE_VALUES: tuple[str, ...] = ("pickup", "jump")
PITCH_RANGE_VALUES: tuple[int, ...] = (8, 16, 100)
KEY_NUDGE_VALUES: tuple[int, ...] = (-1, 1)
KEY_SYNC_MODE_VALUES: tuple[str, ...] = ("exact", "relative", "compatible")
QUANTIZE_GRID_VALUES: tuple[int, ...] = (1, 4, 8)
BEAT_JUMP_SIZE_VALUES: tuple[float, ...] = (0.5, 1, 2, 4, 8, 16, 32, 64)
PAD_MODE_VALUES: tuple[str, ...] = (
    "hot-cue",
    "beat-loop",
    "beat-jump",
    "sampler",
    "pad-fx-1",
    "pad-fx-2",
    "key-shift",
    "keyboard",
)
ANALYSIS_SOURCE_FEATURES: tuple[str, ...] = (
    "beatgrid",
    "key",
    "waveform",
    "loudness",
    "vocal",
    "phrase",
)
ANALYSIS_SOURCE_VALUES: tuple[str, ...] = ("rekordbox", "own")
WAVEFORM_SEEK_SNAP_VALUES: tuple[str, ...] = ("downbeat", "beat", "exact")
COLOR_FX_EFFECT_VALUES: tuple[str, ...] = ("filter", "space", "echo", "crush", "next")
COLOR_FX_ECHO_BEAT_VALUES: tuple[float, ...] = (0.25, 0.5, 0.75, 1.0)
# Keep in sync with BEAT_FX_EFFECT_NAMES in apps/webui/frontend/src/lib/rb/fx/beat-fx-effects/index.ts.
BEAT_FX_EFFECT_VALUES: tuple[str, ...] = (
    "delay",
    "echo",
    "pingpong",
    "spiral",
    "helix",
    "reverse_delay",
    "multitap",
    "tape",
    "filter",
    "roll",
    "crush",
    "reverb",
)
BEAT_FX_TARGET_VALUES: tuple[str, ...] = ("1", "2", "3", "4", "master")
BEAT_FX_BEAT_VALUES: tuple[float, ...] = (
    1 / 16,
    1 / 8,
    1 / 4,
    1 / 2,
    0.75,
    1,
    2,
    4,
    8,
    16,
)
# Keep in sync with TRIPLET_CYCLE_BEATS in apps/webui/frontend/src/lib/rb/mod-fx-lfo.ts (issue #10016).
BEAT_FX_TRIPLET_BEAT_VALUES: tuple[float, ...] = (1 / 12, 1 / 6, 1 / 3, 2 / 3, 4 / 3, 8 / 3, 4)
# Keep in sync with BEAT_FX_RELEASE_KINDS in apps/webui/frontend/src/lib/rb/beat-fx.ts.
BEAT_FX_RELEASE_KIND_VALUES: tuple[str, ...] = ("echo",)
WAVEFORM_DESIGN_VALUES: tuple[str, ...] = ("auto", "tri-band", "mono", "line", "blocks", "rgb")
WAVE_HEIGHT_VALUES: tuple[str, ...] = ("full", "half")
DECK_NUMBER_ART_VALUES: tuple[str, ...] = ("off", "halo", "glow", "tinted")
#: WAVEQS-8348e: mirrors WAVEFORM_OVERLAYS in src/lib/rb/waveform-overlay-command.ts.
WAVEFORM_OVERLAY_VALUES: tuple[str, ...] = (
    "sections",
    "lyrics",
    "lyrics_scrub",
    "jog_radial",
    "hot_cues",
    "memory_cues",
    "loop_cues",
    "vocal_lane",
    "grid_lines",
)
#: issue #10244: mirrors WAVE_GRID_LINES_VALUES in src/lib/rb/wave-grid-lines.ts.
WAVE_GRID_LINES_VALUES: tuple[str, ...] = ("beats_bars", "bars", "off")
WAVE_ZOOM_VALUES: tuple[str, ...] = ("4", "8", "16", "32", "64", "whole")
WAVE_ZOOM_DIRECTION_VALUES: tuple[str, ...] = ("in", "out")
WAVE_ZOOM_PLAYHEAD_VALUES: tuple[str, ...] = ("elastic", "center")
AUTOPLAY_VERDICT_VALUES: tuple[str, ...] = (
    "good",
    "meh",
    "bad",
)  # keep in sync with VERDICTS in lib/rb/autoplay-why.ts
# TOPVIEW-01..03: mirrors PERF_VIEWS in apps/webui/frontend/src/lib/rb/topbar-view-prefs.ts.
TRANSITION_INTENT_VALUES: tuple[str, ...] = ("smooth", "energy_up", "wind_down", "showy", "vocals_clean")
PERF_VIEW_VALUES: tuple[str, ...] = ("waveform", "list", "phase-scope", "phase-meter")
DECK_LAYOUT_VALUES: tuple[str, ...] = ("two-deck", "four-deck")
UI_SKIN_VALUES: tuple[str, ...] = ("default", "mono-dev", "light", "green-room", "practice-ddj-flx4")
WAVE_PALETTE_VALUES: tuple[str, ...] = ("auto", "rekordbox", "legacy", "mono", "blue")
WAVE_SPLIT_MASTER_VALUES: tuple[str, ...] = ("auto", "on", "off")
# Issue #10199: mirrors LAYOUT_SURFACES / PERFORMANCE_LAYOUTS / LIBRARY_LAYOUTS in skin-layout.ts.
LAYOUT_SURFACE_VALUES: tuple[str, ...] = ("performance", "library")
_PERFORMANCE_LAYOUT_VALUES: tuple[str, ...] = ("deck-grid", "replica-desk", "library-first")
_LIBRARY_LAYOUT_VALUES: tuple[str, ...] = ("crate-desk", "three-panel")
LAYOUT_PRESET_VALUES: tuple[str, ...] = ("auto", *_PERFORMANCE_LAYOUT_VALUES, *_LIBRARY_LAYOUT_VALUES)
LAYOUT_REGION_VALUES: tuple[str, ...] = tuple(LAYOUT_REGION_BOUNDS)
# Issue #10263: mirrors SectionJumpDirection in apps/webui/frontend/src/lib/rb/section-jump.ts.
SECTION_JUMP_DIRECTION_VALUES: tuple[str, ...] = ("next", "previous")
#: Issue #9434 / epic #9371: mixer column layout (rendering in MX-2/MX-3).
MIXER_PLACEMENT_VALUES: tuple[str, ...] = ("center", "sides")
MIXER_DECK_PAIR_VALUES: tuple[str, ...] = ("1-3", "2-4")
#: OJ-13717: transition preset ids, in TRANSITION_PRESETS order (transition-presets.ts).
TRANSITION_PRESET_VALUES: tuple[str, ...] = ("bass-swap-8", "filter-out-16", "fader-blend-16")

# The command types AGENT-03 will ramp. An ordering that carries a target must
# be one of these, mirroring the guard in the page's own ramp executor
# (apps/webui/frontend/src/lib/rb/agent-orders.ts). AGENT-24 added crossfader
# and tempo (the canon AutoDJ doc's first gap); tempo travels as a ratio.
RAMPABLE_TYPES: frozenset[str] = frozenset(
    {"eq", "fader", "trim", "filter", "stem_gain", "stem_fx_send", "crossfader", "tempo"}
)


def _stable_ids_value(key: str, raw: str) -> list[str]:
    """Comma-separated track ids for one bus command; an empty id is refused."""
    ids = [part.strip() for part in raw.split(",")]
    if not ids or any(part == "" for part in ids):
        raise ValueError(f"{key} must be comma-separated non-empty ids, got {raw!r}")
    return ids


_DECK = arg("deck", "deck", deck_value, "1-4")
_ENABLED = arg("enabled", "bool", bool_value, "true|false")
_SLOT = arg("slot", "enum", enum_value(SLOT_VALUES), "A-H")
_BAND = arg("band", "enum", enum_value(BAND_VALUES), "low|mid|high")
_STEM = arg("stem", "enum", enum_value(STEM_VALUES), "vocal|instrumental|drums|bass|other")
_UNIT = arg("value", "unit", unit_value, "0..1")
# DIALCLOCK-8341: a bipolar dial also takes a clock time (3pm, 3 o'clock, 15:00; 12 = centre).
_DIAL = arg("value", "dial", dial_value, "0..1|clock")


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
    Verb(
        "load",
        "load",
        (
            _DECK,
            arg("stable_id", "text", text_value),
            arg(
                "replace_playing",
                "bool",
                bool_value,
                "true|false: stop a playing deck and replace its track",
                optional=True,
            ),
        ),
        quick_draws=("load",),
        observes=(Observe(("decks", "{deck}", "stable_id"), "stable_id"),),
        note="AGENT-8332/54: onto a playing deck the load is refused (verdict blocked, exit 6). "
        "replace_playing=true stops the deck and loads; send it only when the user's request "
        "implies replacing a playing deck, otherwise check in with them first.",
    ),
    Verb(
        "load_play_intent",
        "load_play_intent",
        (
            _DECK,
            arg("generation", "int", positive_int_value),
            arg("desired_play", "bool", bool_value, "true|false"),
        ),
    ),
    Verb(
        "unload",
        "unload",
        (_DECK,),
        quick_draws=("unload",),
        observes=(
            Observe(("decks", "{deck}", "title"), NULL),
            Observe(("decks", "{deck}", "stable_id"), NULL),
        ),
    ),
    Verb("play", "play", (_DECK,), fixed=(("playing", True),), quick_draws=("play.toggle",), observes=_deck_playing()),
    Verb(
        "play_quantized",
        "play",
        (_DECK,),
        fixed=(("playing", True), ("quantize", True)),
        observes=(Observe(("decks", "{deck}", "quantized_launch_armed"), PRESENT),),
    ),
    Verb("pause", "play", (_DECK,), fixed=(("playing", False),), observes=_deck_playing()),
    Verb("cue", "cue", (_DECK,), quick_draws=("cue",)),
    Verb(
        "seek",
        "seek",
        (
            _DECK,
            arg("position_ms", "number", number_value),
            arg("exact", "bool", bool_value, "true|false", optional=True),
        ),
        quick_draws=("seek",),
    ),
    Verb(
        "waveform_seek",
        "waveform_seek",
        (
            _DECK,
            arg("position_ms", "number", number_value),
            arg("snap", "enum", enum_value(WAVEFORM_SEEK_SNAP_VALUES), "downbeat|beat|exact"),
        ),
    ),
    Verb(
        "loop",
        "loop",
        (
            _DECK,
            arg("loop", "loop", loop_value, "in_ms out_ms", arity=2),
            arg("quantize", "bool", false_only_value, "false", optional=True),
        ),
        note="Nested pair: opendj loop 1 1000 4000. DECKCTL-6235d: a trailing false engages the range "
        "exactly as given (a stored loop), even with Quantize on; omit it for the default.",
    ),
    Verb("loop_exit", "loop", (_DECK,), fixed=(("loop", None),), quick_draws=("loop.exit",)),
    Verb(
        "beat_loop",
        "beat_loop",
        (
            _DECK,
            arg("beats", "number", number_value),
            arg("start_ms", "number", number_value, optional=True),
        ),
        quick_draws=("loop.start_4", "loop.start_8", "loop.start_16"),
    ),
    Verb(
        "roll",
        "roll",
        (_DECK, arg("beats", "number", roll_beats_value, "1/32|1/16|1/8|1/4|1/2|1|2|4|8|16|32")),
        note="FXROLL-01: a momentary beat loop that forces SLIP; a second roll while held stacks. "
        "End it with roll_release; the deck returns to where the track would have been.",
    ),
    Verb("roll_release", "roll", (_DECK,), fixed=(("beats", None),)),
    Verb("beat_jump", "beat_jump", (_DECK, arg("beats", "number", beat_jump_beats_value, "whole beats or +-0.5"))),
    Verb(
        "beat_jump_size",
        "beat_jump_size",
        (
            _DECK,
            arg("beats", "number", beat_jump_size_value(BEAT_JUMP_SIZE_VALUES), "0.5|1|2|4|8|16|32|64"),
        ),
        note="DECKUX-10470: the size the deck's two beat jump buttons move by (default 4).",
    ),
    Verb(
        "grid_jump",
        "grid_jump",
        (_DECK, arg("bars", "int", int_value)),
        note="DECKUX-6794: snapped jump on the fixed bar grid; -4 back, 4 forward, 8 with Shift.",
    ),
    Verb(
        "section_jump",
        "section_jump",
        (
            _DECK,
            arg("direction", "enum", enum_value(SECTION_JUMP_DIRECTION_VALUES), "next|previous"),
        ),
        note="Issue #10263: seek to the next or previous analysed section start on its downbeat; "
        "a deck with no phrases refuses with NO_SECTIONS (Shift+] and Shift+[ on the page).",
    ),
    Verb(
        "deck_move",
        "deck_move",
        (
            arg("from", "deck", deck_value, "1-4"),
            arg("to", "deck", deck_value, "1-4"),
            arg("crossfade_beats", "number", number_value, "beats, default 8; 0 cuts", optional=True),
            arg("mode", "enum", enum_value(DECK_MOVE_MODE_VALUES), "move|copy", optional=True),
            arg("force", "bool", bool_value, "true|false: move onto a playing deck anyway", optional=True),
        ),
        note="DECKMOVE-01: clone deck <from> (track, playhead on the audio clock, live loop, tempo, key, "
        "EQ, stems, colour FX) onto <to>; move crossfades then unloads <from>, copy leaves it playing.",
    ),
    Verb("loop_interval_mode", "loop_interval_mode", (_DECK, _ENABLED)),
    Verb(
        "pad_mode",
        "pad_mode",
        (_DECK, arg("mode", "enum", enum_value(PAD_MODE_VALUES), "|".join(PAD_MODE_VALUES))),
        note="PADFX-11718: the deck's performance-pad mode (HOT CUE menu); Pad FX refuses with the beat FX unit's reason.",
        observes=(Observe(("decks", "{deck}", "pad_mode", "selected"), "mode"),),
    ),
    Verb("loop_interval_base", "loop_interval_base", (_DECK, arg("base", "number", number_value))),
    Verb(
        "tempo",
        "tempo",
        (_DECK, arg("ratio", "number", number_value)),
        quick_draws=("tempo.reset",),
        observes=(Observe(("decks", "{deck}", "pitch"), "ratio", 1e-6),),
    ),
    Verb(
        "pitch_range",
        "pitch_range",
        (
            _DECK,
            arg("range", "int", int_enum_value(PITCH_RANGE_VALUES), "8|16|100"),
        ),
    ),
    # ----- deck mode toggles -------------------------------------------
    Verb(
        "quantize",
        "quantize",
        (
            _DECK,
            _ENABLED,
            arg("by_user", "bool", bool_value, "true|false", optional=True),
        ),
        quick_draws=("quantize.toggle",),
        note="DECKUX-39: an off is refused (422) unless by_user is true (a person asked); "
        "a user off holds until the deck's next load.",
    ),
    Verb(
        "quantize_grid",
        "quantize_grid",
        (
            _DECK,
            arg("beats", "quantize_grid", quantize_grid_value(QUANTIZE_GRID_VALUES), "1|4|8|phase"),
        ),
        note="DECKCTL-6235c: phase snaps to phrase starts (the bar, 4 beats, on a track with no phrases).",
    ),
    Verb(
        "beat_sync",
        "beat_sync",
        (_DECK, _ENABLED),
        quick_draws=("beat_sync.toggle",),
        observes=(Observe(("decks", "{deck}", "sync", "enabled"), "enabled"),),
    ),
    Verb(
        "sync_mode",
        "sync_mode",
        (
            _DECK,
            arg("mode", "enum", enum_value(SYNC_MODE_VALUES), "beat|bar"),
        ),
        quick_draws=("sync_mode.beat", "sync_mode.bar"),
        observes=(Observe(("decks", "{deck}", "sync", "mode"), "mode"),),
    ),
    Verb("master", "master", (_DECK,), quick_draws=("master",)),
    Verb("master_tempo", "master_tempo", (_DECK, _ENABLED), quick_draws=("master_tempo.toggle",)),
    Verb("stem_mute", "stem_mute", (_DECK, _STEM, arg("muted", "bool", bool_value, "true|false"))),
    Verb("stem_solo", "stem_solo", (_DECK, _STEM, arg("solo", "bool", bool_value, "true|false"))),
    Verb(
        "stem_load",
        "stem_load",
        (_DECK,),
        note="STEM-46/47: get the deck's stems now (retry a failed load, start a held one).",
    ),
    Verb(
        "track_info_open",
        "track_info_open",
        (
            arg("deck", "deck", deck_value, "1-4", optional=True),
            arg("stable_id", "text", text_value, optional=True),
        ),
        note="Opens the Track info overlay for a library track (stable_id) or that "
        "deck's loaded track and selects the deck (#9448). Give one of deck or stable_id.",
    ),
    Verb(
        "stem_separate",
        "stem_separate",
        (_DECK, arg("stable_id", "text", text_value, optional=True)),
        note="STEM-77: enqueue separation of the track on a NO STEMS deck; "
        "refused while a job for it is queued or running, or when the deck no longer "
        "holds stable_id (omitted: the track it holds when the command arrives). "
        "stem_load lands the result.",
    ),
    Verb(
        "stem_eq_mode",
        "stem_eq_mode",
        (_DECK, _ENABLED),
        observes=(Observe(("mixer", "channels", "{deck}", "stem_eq_mode"), "enabled"),),
    ),
    Verb(
        "stem_gain",
        "stem_gain",
        (_DECK, _STEM, _UNIT),
        observes=(Observe(("decks", "{deck}", "stems", "controls", "{stem}", "gain"), "value", 1e-6),),
    ),
    Verb(
        "stem_fx_send",
        "stem_fx_send",
        (_DECK, _STEM, _UNIT),
        observes=(Observe(("decks", "{deck}", "stems", "controls", "{stem}", "fx_send"), "value", 1e-6),),
        note="STEMPERF-11663: per-stem send (0..1) into the channel's colour FX input; heard on SPACE/ECHO.",
    ),
    Verb("slip", "slip", (_DECK, _ENABLED), quick_draws=("slip.toggle",)),
    Verb(
        "key_sync",
        "key_sync",
        (
            _DECK,
            _ENABLED,
            arg("mode", "enum", enum_value(KEY_SYNC_MODE_VALUES), "exact|relative|compatible", optional=True),
            arg("target", "deck_or_master", deck_or_master_value, "1-4|master", optional=True),
        ),
        quick_draws=("key_sync.toggle",),
        note="KEYSYNC-01: exact (default) transposes onto the target key, shortest path -6..+6; "
        "relative lands on its Camelot number; target defaults to MASTER. Turns key lock on.",
    ),
    Verb(
        "key_shift",
        "key_shift",
        (
            _DECK,
            arg("semitones", "int", int_value, "-12..12"),
        ),
        note="KEYSYNC-01 ladder: absolute key shift (0 = original key); key lock on, KEY SYNC off. "
        "decks.<n>.key_options lists each shift with its landed key and Mixed In Key pattern.",
    ),
    Verb(
        "key_nudge",
        "key_nudge",
        (
            _DECK,
            arg("semitones", "int", int_enum_value(KEY_NUDGE_VALUES), "-1|1"),
        ),
        quick_draws=("key_nudge.up", "key_nudge.down"),
    ),
    # ----- mixer strip --------------------------------------------------
    Verb("trim", "trim", (_DECK, _DIAL), observes=_mixer("trim", "value")),
    Verb(
        "auto_gain_offset",
        "auto_gain_offset",
        (
            _DECK,
            arg("offset_db", "number", auto_gain_offset_value, "-12..12|clear"),
            arg("stable_id", "text", text_value),
        ),
        note="AUTOGAIN-04: the loaded track's own auto gain dB; clear uses the analysed value. "
        "stable_id names the track meant; a deck holding another one refuses the write.",
    ),
    Verb(
        "eq",
        "eq",
        (_DECK, _BAND, _DIAL),
        observes=(Observe(("mixer", "channels", "{deck}", "eq_{band}"), "value", 1e-6),),
    ),
    Verb("filter", "filter", (_DECK, _DIAL), observes=_mixer("filter", "value")),
    Verb("fader", "fader", (_DECK, _UNIT), observes=_mixer("fader", "value")),
    Verb(
        "assign",
        "assign",
        (_DECK, arg("assign", "enum", enum_value(ASSIGN_VALUES), "A|B|THRU")),
        observes=_mixer("assign", "assign"),
    ),
    Verb("channel_cue", "channel_cue", (_DECK, _ENABLED), observes=_mixer("cue_enabled", "enabled")),
    Verb(
        "level_calibration",
        "level_calibration",
        (_DECK, arg("kind", "enum", enum_value(("red", "ceiling")), "red|ceiling"), _ENABLED),
        note="#5586: the mixer's R|M level calibration (red anchor, master ceiling), also while "
        "LESS hides it. Arming captures the deck's level and is refused while the deck is stopped.",
    ),
    Verb("crossfader", "crossfader", (_UNIT,), observes=(Observe(("mixer", "crossfader"), "value", 1e-6),)),
    Verb("master_volume", "master_volume", (_UNIT,), observes=(Observe(("master", "level"), "value", 1e-6),)),
    Verb(
        "master_mute",
        "master_mute",
        (arg("muted", "bool", bool_value, "true|false"),),
        observes=(Observe(("master", "muted"), "muted"),),
    ),
    Verb("headphone_mix", "headphone_mix", (_UNIT,)),
    Verb("headphone_level", "headphone_level", (_UNIT,)),
    Verb(
        "headphone_booth_level",
        "headphone_booth_level",
        (_UNIT,),
        note="CUEOUT-13: BOOTH gain only; MAIN and HEADPHONE CUE are unchanged.",
    ),
    Verb(
        "booth_level",
        "booth_level",
        (_UNIT,),
        note="CUEOUT-12100: booth monitor gain; heard as-is when signal_mode is independent.",
    ),
    Verb(
        "booth_signal_mode",
        "booth_signal_mode",
        (arg("mode", "enum", enum_value(BOOTH_SIGNAL_MODE_VALUES), "off|follows_master|independent"),),
        note="CUEOUT-12100: off mutes the booth; follows_master scales it by the room master level.",
    ),
    Verb(
        "head_delay_ms",
        "head_delay_ms",
        (arg("value", "number", head_delay_ms_value, "0..500"),),
        note="CUEOUT-03: cue-monitor delay in milliseconds.",
    ),
    Verb(
        "headphone_alignment_mode",
        "headphone_alignment_mode",
        (arg("value", "enum", enum_value(HEADPHONE_ALIGNMENT_MODE_VALUES), "headphones_only|delay_all|hybrid"),),
        note="CUEOUT-14: how a measured cue/master offset is split; re-applies the last calibration.",
    ),
    Verb(
        "headphone_calibration_probe",
        "headphone_calibration_probe",
        (arg("value", "enum", enum_value(CALIBRATION_PROBE_VALUES), "chirp|click_train"),),
        note="CUEOUT-12692: calibration stimulus for the next run; refused while one is running.",
    ),
    Verb(
        "master_delay_ms",
        "master_delay_ms",
        (arg("value", "number", master_delay_ms_value, "0..1500"),),
        note="CUEOUT-14: room (MASTER) delay in milliseconds, the last node before the output.",
    ),
    Verb(
        "headphone_calibrate",
        "headphone_calibrate",
        (),
        note="CUEOUT-14: headless mic calibration; holds until applied or failed.",
    ),
    Verb(
        "headphone_calibrate_abort",
        "headphone_calibrate_abort",
        (),
        note="CUEOUT-14: stop an in-flight calibration; decks resume, nothing is applied.",
    ),
    # ----- browser, library and headphone outputs -----------------------
    Verb(
        "browser_select_playlist",
        "browser_select_playlist",
        (
            arg("playlist_id", "text", text_value),
            arg("stable_id", "text", text_value, "also select and scroll to this track", optional=True),
        ),
        observes=(Observe(("browser", "playlist"), "playlist_id"),),
        note="LIBUX/PLAYLIST: selected pane; confirmed against mirror browser.playlist.",
    ),
    Verb(
        "browser_related",
        "browser_related",
        (arg("active", "bool", bool_value, "true: show the Related tab, false: leave it"),),
        note="LIBUX-10046: show or leave the library's Related tab.",
    ),
    Verb(
        "browser_related_criteria",
        "browser_related_criteria",
        (
            arg("bpm_percent", "int", related_bpm_percent_value, "1..20", optional=True),
            arg("key", "bool", bool_value, "true|false", optional=True),
            arg("genre", "bool", bool_value, "true|false", optional=True),
            arg("color", "bool", bool_value, "true|false", optional=True),
            arg("rating", "bool", bool_value, "true|false: equal or higher", optional=True),
            arg("mytag", "bool", bool_value, "true|false", optional=True),
            arg("artist", "bool", bool_value, "true|false", optional=True),
        ),
        note="LIBUX-10046: edit Related Tracks criteria, e.g. browser_related_criteria artist=true bpm_percent=4.",
    ),
    Verb(
        "browser_related_criteria_reset",
        "browser_related_criteria_reset",
        (),
        note="LIBUX-10046: restore the default Related criteria (bpm 6 percent, key, genre).",
    ),
    Verb(
        "browser_tree_search",
        "browser_tree_search",
        (arg("query", "text", text_value, "playlist or folder name fragment"),),
        note="LIBM-87: filter the playlist tree by name, as its search box does; the tree itself is unchanged.",
    ),
    Verb(
        "browser_tree_search_clear",
        "browser_tree_search_clear",
        (),
        note="LIBM-87: clear the playlist tree search; the tree shows its earlier open state and selection.",
    ),
    Verb(
        "browser_prev_track",
        "browser_step_selection",
        (),
        fixed=(("delta", -1),),
        note="LIBUX (issue #9744): the pane tab's previous button; selection up one row (last row when none), "
        "clamped; read it back as browser.selected_row.",
    ),
    Verb(
        "browser_next_track",
        "browser_step_selection",
        (),
        fixed=(("delta", 1),),
        note="LIBUX (issue #9744): the pane tab's next button; selection down one row (first row when none), "
        "clamped; read it back as browser.selected_row.",
    ),
    Verb(
        "tag_list_add",
        "tag_list_add",
        (arg("stable_ids", "text", _stable_ids_value, "id[,id...]"),),
        note="Issue #10028: Add to Tag List from the browser (the row menu's path), comma-separated ids; "
        "the browser calls POST /api/v1/taglist/items and its rail badge follows. Without a browser use "
        "opendj taglist add.",
    ),
    Verb(
        "browser_type_ahead",
        "browser_type_ahead",
        (arg("prefix", "text", text_value, "title prefix"),),
        note="LIBM-82b (issue #12317): jump the selection to the next visible row whose title starts with "
        "prefix; the search and filters stay. A repeated single letter cycles; no match is refused.",
    ),
    Verb(
        "library_filter",
        "library_filter",
        (),
        fixed=(("op", "get"),),
        note="LIBUX (issue #10043): read the mounted pane's Track Filter bar state; it is browser.filter "
        "in the returned state.",
    ),
    Verb(
        "library_filter_set",
        "library_filter",
        (arg("state", "json", json_object_value, "JSON object"),),
        fixed=(("op", "set"),),
        note="LIBUX (issue #10043): replace the Track Filter bar state, e.g. library_filter_set "
        '\'{"bpm":{"min":120,"max":128},"suspended":false}\'. An unknown field or BPM min above max '
        "is refused.",
    ),
    Verb("headphone_outputs_refresh", "headphone_outputs_refresh", ()),
    # CUEOUT-15: the mini-waveform click, without a pointer. It loads no deck
    # and moves no transport, so it is issuable while all four decks are busy
    # and while one of them is on air. `bpm` is the previewed track's own
    # tempo; supply it and the preview tempo-matches a playing master deck
    # when the setting is on and the match fits the preview pitch range.
    Verb(
        "preview_cue",
        "preview_cue",
        (
            arg("stable_id", "text", text_value),
            arg("ratio", "unit", unit_value, "0..1"),
            arg("bpm", "number", positive_number_value, "track BPM", optional=True),
        ),
        note="CUEOUT-15: play a library track on the cue bus from a point in it.",
    ),
    Verb(
        "preview_stop",
        "preview_stop",
        (),
        note="CUEOUT-15: stop the preview and release the decoded track; idempotent.",
    ),
    Verb("headphone_output_acquire", "headphone_output_acquire", ()),
    Verb("headphone_output_select", "headphone_output_select", (arg("device_id", "text", text_value),)),
    Verb(
        "headphone_master_select",
        "headphone_master_select",
        (arg("device_id", "text", text_value),),
        note="CUEOUT-09: pin AudioContext master sink.",
    ),
    Verb(
        "headphone_booth_select",
        "headphone_booth_select",
        (arg("device_id", "text", text_value),),
        note="CUEOUT-13: pin BOOTH to the room mix; an empty id unsets it.",
    ),
    Verb(
        "booth_select",
        "booth_select",
        (arg("device_id", "text", text_value),),
        note="CUEOUT-12100: pin the booth output device (mixer.booth.device_id).",
    ),
    Verb(
        "headphone_input_select",
        "headphone_input_select",
        (arg("device_id", "text", text_value),),
        note="CUEOUT-10: audio input; default is never a headphone/HFP mic.",
    ),
    Verb(
        "output_mode",
        "output_mode",
        (arg("mode", "enum", enum_value(OUTPUT_MODE_VALUES), "practice|two_outputs|split_cable"),),
        note=(
            "CUEOUT-01/02: practice blends PFL into the main output; two_outputs is "
            "MAIN+CUE devices; split_cable is master L / cue R on one device."
        ),
    ),
    Verb(
        "midi_takeover_mode",
        "midi_takeover_mode",
        (arg("mode", "enum", enum_value(MIDI_TAKEOVER_MODE_VALUES), "pickup|jump"),),
        note="IOPIN-06: pickup holds a MIDI dial until the hardware crosses the software value.",
    ),
    Verb(
        "midi_displaced_warning",
        "midi_displaced_warning",
        (_ENABLED,),
        note="PVPIN-12193: the orange warning while a MIDI knob or slider stays displaced; true turns it back on.",
    ),
    Verb("midi_learn_retry", "midi_learn_retry", ()),
    Verb("midi_learn_cancel", "midi_learn_cancel", ()),
    Verb(
        "analysis_source",
        "analysis_source",
        (
            arg(
                "feature",
                "enum",
                enum_value(ANALYSIS_SOURCE_FEATURES),
                "beatgrid|key|waveform|loudness|vocal|phrase",
            ),
            arg("source", "enum", enum_value(ANALYSIS_SOURCE_VALUES), "rekordbox|own"),
        ),
        note="PARITY-02: rbx-vs-own analysis source A/B per feature.",
    ),
    Verb(
        "auto_play_two_track",
        "auto_play_two_track",
        (_ENABLED,),
        note="TOPVIEW-06: two-track AutoPlay keeps the set on decks 1 and 2.",
    ),
    Verb(
        "set_perf_view",
        "set_perf_view",
        (arg("view", "enum", enum_value(PERF_VIEW_VALUES), "waveform|list|phase-scope|phase-meter"),),
        note="TOPVIEW-01..03: the top bar's view icons; what the wavestack area shows.",
    ),
    Verb(
        "set_deck_layout",
        "set_deck_layout",
        (arg("layout", "enum", enum_value(DECK_LAYOUT_VALUES), "two-deck|four-deck"),),
        note="Issue #12204: the top bar's 2-deck toggle; read back as ui.deck_layout.",
    ),
    Verb(
        "toggle_large_library",
        "toggle_large_library",
        (),
        note="Issue #9432: right Option's large library toggle; read back as ui.large_library.",
    ),
    Verb(
        "show_pads",
        "show_pads",
        (_ENABLED,),
        note="TOPVIEW-04: the top bar's PAD button; show or hide the performance-pad rows.",
    ),
    Verb(
        "set_lyrics_global",
        "set_lyrics_global",
        (arg("enabled", "bool", bool_value, "on|off|true|false"),),
        note="Issue #13057: the top bar's LYR master switch for every lyric overlay; persists lyrics_global "
        "only, so set_waveform_overlay lyrics and the other lyric prefs keep their own state.",
    ),
    Verb(
        "key_nudge_step",
        "key_nudge_step",
        (arg("step", "enum", enum_value(("semitone", "tone", "scale", "camelot")), "semitone|tone|scale|camelot"),),
        note="DECKGAP-7691b: the key nudge arrows' step size, shared by every deck (camelot = one fifth).",
    ),
    Verb(
        "track_load_reset",
        "track_load_reset",
        tuple(arg(item, "bool", bool_value, "true|false") for item in ("level", "stems", "eq", "trim", "filter")),
        note="DECKGAP-7691: which mixer values return to neutral when a track loads (all five, every deck).",
    ),
    Verb(
        "reset_deck_now",
        "reset_deck_now",
        (
            _DECK,
            arg("parts", "reset_parts", reset_parts_value, "level,stems,eq,trim,filter", optional=True),
        ),
        note="DECKGAP-7691h: the reset panel's Reset now; apply the ticked resets (or the named parts) to this deck at once.",
    ),
    Verb(
        "auto_play_next_arm",
        "auto_play_next_arm",
        (
            arg(
                "intent",
                "enum",
                enum_value(TRANSITION_INTENT_VALUES),
                "smooth|energy_up|wind_down|showy|vocals_clean",
                optional=True,
            ),
            arg("out_deck", "deck", deck_value, "1-4", optional=True),
            arg("in_deck", "deck", deck_value, "1-4", optional=True),
            arg("from_ms", "number", number_value, "ms on the outgoing track", optional=True),
        ),
        note="AGENT-48: plan the next transition from the live position; the fixed loop runs when no plan is eligible. "
        "AGENT-50: out_deck, in_deck and from_ms plan out of that deck around that point (no fallback).",
    ),
    Verb("auto_play_next_cancel", "auto_play_next_cancel", ()),
    Verb(
        "auto_play_decide_now",
        "auto_play_decide_now",
        (),
        note="AUTOPLAN-06: AutoPlay decides now rather than at its target; read autoplay_decision on the mirror.",
    ),
    Verb(
        "auto_play_next_intent",
        "auto_play_next_intent",
        (arg("intent", "enum", enum_value(TRANSITION_INTENT_VALUES), "smooth|energy_up|wind_down|showy|vocals_clean"),),
        note="The AutoPlay menu Intent selector: set the next planned transition's intent without arming.",
    ),
    Verb(
        "autoplay_verdict",
        "autoplay_verdict",
        (
            arg("out", "text", text_value, "outgoing stable_id"),
            arg("in", "text", text_value, "incoming stable_id"),
            arg("verdict", "enum", enum_value(AUTOPLAY_VERDICT_VALUES), "good|meh|bad"),
        ),
        note="ADR-0139: the AutoPlay menu's one-tap verdict on how the out -> in transition sounded. "
        "Pure logging, the planner never reads it; opendj dj verdict is the HTTP twin.",
    ),
    Verb(
        "auto_play_early",
        "auto_play_early",
        (
            arg("enabled", "bool", bool_value, "true|false", optional=True),
            arg("decide_after_s", "number", number_value, "seconds on the master", optional=True),
        ),
        note="AUTOPLAN-01: AutoPlay's early planning and its soft target; the AutoPlay menu's twin.",
    ),
    Verb(
        "pins_show_other_users",
        "pins_show_other_users",
        (),
        note="UI contract only: community pin sync has no channel yet.",
    ),
    Verb(
        "library_panels",
        "library_panels",
        (
            arg("panel", "enum", enum_value(PANEL_VALUES), "next|recommended"),
            arg("collapsed", "bool", bool_value, "true|false"),
        ),
    ),
    Verb(
        "library_panels_toggle",
        "library_panels_toggle",
        (arg("collapsed", "bool", bool_value, "true|false", optional=True),),
        note="LIBUX-02: the combined Next/Recommended chevron; omit collapsed to flip it.",
    ),
    Verb(
        "library_single_column",
        "library_single_column",
        (arg("enabled", "bool", bool_value, "true|false", optional=True),),
        note="LIBUX (issue #9743): the browser's single-column button; hides the playlist tree so the track "
        "table fills the width, and persists. Omit enabled to flip it; read it back as "
        "library_panels.single_column.",
    ),
    Verb(
        "library_split_column",
        "library_split_column",
        (arg("enabled", "bool", bool_value, "true|false", optional=True),),
        note="LIBUX (issue #9754): the browser's split-column button; two track tables side by side, each on its "
        "own pane with its own source, sort and selection, and persists. Omit enabled to flip it; read it back "
        "as library_panels.split_column.",
    ),
    Verb(
        "library_mytag_pane",
        "library_mytag_pane",
        (arg("enabled", "bool", bool_value, "true|false", optional=True),),
        note="LIBUX (issue #10026): the browser's My Tag pane toggle; docks the tag checkboxes right of the "
        "track table, and persists. Omit enabled to flip it; read it back as library_panels.mytag_pane. "
        "Tags themselves are written with POST /api/v1/mytags/assignments.",
    ),
    Verb(
        "deck_autocue",
        "deck_autocue",
        (_ENABLED,),
        observes=(Observe(("deck_autocue",), "enabled"),),
        note="DECKCTL-6235: the AU switch; a fresh load parks at its first memory cue, else its first beat.",
    ),
    Verb(
        "color_fx",
        "color_fx",
        (
            arg("deck", "deck", deck_or_all_value, "1-4|all"),
            arg("effect", "enum", enum_value(COLOR_FX_EFFECT_VALUES), "filter|space|echo|crush|next", optional=True),
            arg("echo_beats", "number", number_enum_value(COLOR_FX_ECHO_BEAT_VALUES), "0.25|0.5|0.75|1", optional=True),
            arg("value", "dial", dial_value, "0..1|clock", optional=True),
        ),
        note="COLORFX-01..05: pick what the channel color knob plays (FILTER, SPACE reverb or a "
        "beat-synced dub ECHO timed to the deck tempo, or SNDCOL-10655 CRUSH: bit depth and sample rate drop "
        "counterclockwise, clockwise a fixed crush through a rising high-pass), the echo division in beats, and "
        "optionally the knob itself (0.5 = center, exactly dry). Sticky across track loads; "
        "the mirror's color_fx carries it per deck. Refused in Rust engine mode. "
        "COLORFX-8341: effect=next cycles FILTER, SPACE, ECHO, CRUSH; switching effect removes the "
        "old one and returns the knob to 12 o'clock unless value is given. DIALCLOCK-8341: "
        "value takes a clock time (9 o'clock = 0.1667, 12 = centre, 3 o'clock = 0.8333; "
        "the dial runs 7:30 to 4:30). COLORFX-13: deck=all puts one effect on channels 1 to 4 "
        "(rekordbox single mode); each channel follows the switch rule; value and effect=next "
        "are refused with deck=all.",
    ),
    Verb(
        "beat_fx",
        "beat_fx",
        (
            arg("target", "enum", enum_value(BEAT_FX_TARGET_VALUES), "1|2|3|4|master", optional=True),
            arg("effect", "enum", enum_value(BEAT_FX_EFFECT_VALUES), "|".join(BEAT_FX_EFFECT_VALUES), optional=True),
            arg(
                "beats",
                "number",
                fraction_enum_value(BEAT_FX_BEAT_VALUES + BEAT_FX_TRIPLET_BEAT_VALUES),
                "1/16..16, or 1/12..4 with triplet=true",
                optional=True,
            ),
            arg("triplet", "bool", bool_value, "true|false (default false)", optional=True),
            arg("time_ms", "number", number_value, "1..4000 ms, not with beats", optional=True),
            arg("level", "dial", dial_value, "0..1", optional=True),
            arg("enabled", "bool", bool_value, "true|false", optional=True),
            arg("quantize", "bool", bool_value, "true|false", optional=True),
        ),
        note="BEATFX-12: the channel-select beat FX unit (DELAY, ECHO, SPIRAL, HELIX, FILTER, ROLL, CRUSH and the "
        "rest of the beat FX registry; one with no kernel on main is refused by name). The mirror "
        "beat_fx field reports effect and division. Refused while fx.beat_rack is off, and in "
        "Rust engine mode. BEATFX-10001: time_ms sets a free TIME (1..4000 ms, the effect's catalog "
        "range) for the delay family; time_ms and beats in one command are refused, and beats again "
        "returns to synced timing. BEATFX-10016: triplet=true takes beats from the triplet set "
        "1/12, 1/6, 1/3, 2/3, 4/3, 8/3, 4 (12/3), written as a/b; a duple-only beats with triplet=true "
        "is refused, and the unit refuses triplet until the triplet kernels are wired.",
    ),
    Verb(
        "beat_fx_release",
        "beat_fx_release",
        (
            arg("held", "bool", bool_value, "true|false"),
            arg("beats", "number", number_enum_value(BEAT_FX_BEAT_VALUES), "1/16..16", optional=True),
            arg("kind", "enum", enum_value(BEAT_FX_RELEASE_KIND_VALUES), "echo", optional=True),
        ),
        observes=(Observe(("beat_fx", "release_held"), "held"),),
        note="BEATFX-12: hold (held=true) or let go of Release FX, an echo of the beat FX target "
        "while the dry cuts; holding twice or letting go unheld is refused. The mirror beat_fx "
        "field reports release_held. Refused while fx.beat_rack is off, and in Rust engine mode.",
    ),
    Verb("show_stems", "show_stems", (_ENABLED,), note="DECKUX-19: per-stem mini-waveforms under the deck wavestack."),
    Verb(
        "wave_eq_reflect",
        "wave_eq_reflect",
        (_ENABLED,),
        note="WAVE-EQ-5230: deck waveforms follow the engine-reported EQ/filter gains.",
    ),
    Verb(
        "mixer_stems",
        "mixer_stems",
        (_ENABLED,),
        note="The mixer's S button: VOCAL/INST/BASS dials beside HI/MID/LOW and DRUMS beside FILTER "
        "on every channel with stems; read back as ui.mixer_stems.",
    ),
    Verb(
        "autoplay",
        "autoplay",
        (
            _ENABLED,
            arg("by_user", "bool", bool_value, "true|false", optional=True),
        ),
        observes=(Observe(("autoplay_enabled",), "enabled"),),
        note="AGENT-20: the AutoPlay switch; the mirror's autoplay_armed says if it will hand off. "
        "PLAY-18: off is refused unless by_user is true (a person asked).",
    ),
    Verb(
        "set_deck_number_art",
        "set_deck_number_art",
        (arg("mode", "enum", enum_value(DECK_NUMBER_ART_VALUES), "off|halo|glow|tinted"),),
        note="ARTNUM-9649: deck number outline over each deck's artwork; default off. "
        "ARTNUM-11719: glow and tinted take a colour sampled from the artwork.",
    ),
    Verb(
        "set_waveform_design",
        "set_waveform_design",
        (
            arg(
                "design",
                "enum",
                enum_value(WAVEFORM_DESIGN_VALUES),
                "auto|tri-band|mono|line|blocks|rgb",
                optional=True,
            ),
            arg("height", "enum", enum_value(WAVE_HEIGHT_VALUES), "full|half", optional=True),
        ),
        note="WAVEQS-11733: height full mirrors the deck waveform, half draws it single-sided from the bottom "
        "baseline (CDJ style); name it alone as height=half. The page refuses a call with neither.",
    ),
    Verb(
        "set_waveform_overlay",
        "set_waveform_overlay",
        (
            arg(
                "overlay",
                "enum",
                enum_value(WAVEFORM_OVERLAY_VALUES),
                "|".join(WAVEFORM_OVERLAY_VALUES),
            ),
            arg("enabled", "bool", bool_value, "true|false (the switch overlays)", optional=True),
            arg(
                "value",
                "enum",
                enum_value(WAVE_GRID_LINES_VALUES),
                "beats_bars|bars|off (grid_lines only)",
                optional=True,
            ),
        ),
        note="WAVEQS-8348e: the waveform quick-settings popover's switches: sections, lyrics on the waveform, "
        "lyrics while hover-scrubbing, the radial jog waveform, hot cue, memory cue and loop markers (#10248). "
        "WAVEQS-11734: vocal_lane shows or hides the blue vocal bars. "
        "WAVEQS-11736: grid_lines takes value=beats_bars|bars|off (beat and bar lines, bar lines only, none), "
        "as in: set_waveform_overlay grid_lines value=bars.",
    ),
    Verb(
        "set_wave_zoom",
        "set_wave_zoom",
        (
            arg("zoom", "enum", enum_value(WAVE_ZOOM_VALUES), "4|8|16|32|64|whole (bars across the row)"),
            arg("deck", "deck", deck_value, "1-4", optional=True),
        ),
        note="WAVEZOOM-6802: main waveform zoom; deck only matters while the zoom is unlinked.",
    ),
    Verb(
        "wave_zoom_step",
        "wave_zoom_step",
        (
            arg("direction", "enum", enum_value(WAVE_ZOOM_DIRECTION_VALUES), "in|out"),
            arg("deck", "deck", deck_value, "1-4", optional=True),
        ),
        note="WAVEZOOM-6802b: one ladder step, as Ctrl/Cmd-wheel, pinch and the [ ] keys do.",
    ),
    Verb(
        "set_wave_zoom_options",
        "set_wave_zoom_options",
        (
            arg("playhead", "enum", enum_value(WAVE_ZOOM_PLAYHEAD_VALUES), "elastic|center"),
            arg("linked", "bool", bool_value, "true|false"),
        ),
        note="WAVEZOOM-6802c/06: playhead glide when zoomed out; one zoom for all decks or one each.",
    ),
    Verb(
        "set_skin",
        "set_skin",
        (
            arg("ui_skin", "enum", enum_value(UI_SKIN_VALUES), "default|mono-dev|light|green-room|practice-ddj-flx4"),
            arg("wave_palette", "enum", enum_value(WAVE_PALETTE_VALUES), "auto|rekordbox|legacy|mono|blue"),
            arg("wave_split_master", "enum", enum_value(WAVE_SPLIT_MASTER_VALUES), "auto|on|off"),
        ),
        note="Skin preview: chrome skin, waveform palette and split master waveform in one call. "
        "wave_split_master is opt-in: auto = off on every skin (mirrored rows), on = split.",
    ),
    Verb(
        "set_layout_preset",
        "set_layout_preset",
        (
            arg("surface", "enum", enum_value(LAYOUT_SURFACE_VALUES), "performance|library"),
            arg(
                "preset",
                "enum",
                enum_value(LAYOUT_PRESET_VALUES),
                "auto|deck-grid|replica-desk|library-first|crate-desk|three-panel",
            ),
        ),
        note="Issue #10199: any skin runs any surface preset; auto follows the skin's layout. "
        "The bus refuses a preset of the other surface with the allowed list; read back as "
        "ui.layout (effective) and ui.layout_override (stored).",
    ),
    Verb(
        "set_layout_size",
        "set_layout_size",
        (
            arg("region", "enum", enum_value(LAYOUT_REGION_VALUES), "browser.tree"),
            arg("px", "number", layout_px_value, "220..520"),
        ),
        observes=(Observe(("layout", "regions", "{region}", "px"), "px"),),
        note="DL-2 (#9429): a layout region's size in CSS px, as dragging its handle does. "
        "Out of range is refused with the allowed range, never clamped.",
    ),
    Verb(
        "reset_layout",
        "reset_layout",
        (arg("region", "enum", enum_value(LAYOUT_REGION_VALUES), "browser.tree", optional=True),),
        note="DL-2 (#9429): restore one region's skin default size, or every region when none is named.",
    ),
    Verb(
        "grid_edit",
        "grid_edit",
        (
            _DECK,
            arg("edit", "grid_edit", grid_edit_value, "adjust|shift|<offset_ms>[:<beats>]"),
        ),
        note="GRIDEDIT-6235d: nudge the loaded track's stored grid (ms, plus whole beats) or reset "
        "the adjust or shift part; the same queue the deck's grid buttons use. "
        "BEATGRID-13519: a constant-tempo override at a served anchor is "
        "`opendj track grid-edit <stable_id> bpm <bpm> <anchor_t>` (PUT /grid-edit), not a bus verb.",
    ),
    Verb("feedback_mark", "feedback_mark", (arg("vote", "enum", enum_value(VOTE_VALUES), "bad|good|great"),)),
    # RESCUE-01 HTTP parity (not command-bus verbs):
    #   opendj api GET /api/v1/performance/rescue-snapshots/latest
    #   opendj api POST /api/v1/performance/rescue-snapshots --json @snapshot.json
    # AUDIO-DEVICE-01 HTTP parity (issue #923, not command-bus verbs):
    #   opendj audio_output_health
    #   opendj audio_switch_output --confirm
    #   opendj api GET /api/v1/audio/output-health
    #   opendj api POST /api/v1/audio/switch-output
    # AUDIO-DEVICE-05 HTTP parity (Reconnect speaker, not a command-bus verb):
    #   opendj audio_reconnect_speaker --confirm
    #   opendj api POST /api/v1/audio/reconnect-speaker
    # PERFREADOUT-12579 HTTP parity (top-bar perf card rows, read-only):
    #   opendj perf_readouts
    #   opendj api GET /api/v1/performance/telemetry/readouts
    # FB-20 HTTP parity (issue #4085, not command-bus verbs):
    #   opendj feedback comments summary
    #   opendj api GET /api/v1/feedback/comments/summary
    # ----- safety loop and hot cues -------------------------------------
    Verb("safety_loop_save", "safety_loop_save", (_DECK,)),
    Verb("safety_loop_arm", "safety_loop_arm", (_DECK, arg("armed", "bool", bool_value, "true|false"))),
    Verb("safety_loop_clear", "safety_loop_clear", (_DECK,)),
    Verb(
        "hot_cue_save",
        "hot_cue_save",
        (
            _DECK,
            _SLOT,
            arg("in_ms", "number", number_value),
            arg("revision", "text", text_value),
            arg("comment", "text", text_value, optional=True),
            # HOTCUE-LOOP-01: a loop hot cue's end, named: `... out_ms=31875`.
            arg("out_ms", "number", number_value, optional=True),
        ),
    ),
    Verb("hot_cue_clear", "hot_cue_clear", (_DECK, _SLOT, arg("revision", "text", text_value))),
    Verb(
        "hot_cue_restore",
        "hot_cue_restore",
        (
            _DECK,
            _SLOT,
            arg("revision", "text", text_value),
            arg("reversal_id", "text", text_value),
        ),
    ),
    Verb("hot_cue_trigger", "hot_cue_trigger", (_DECK, _SLOT)),
    # CUES-10639: the deck loop menu's Set active / Clear active flag.
    Verb(
        "active_memory_loop",
        "active_memory_loop",
        (
            _DECK,
            arg("in_ms", "number", number_value),
            arg("out_ms", "number", number_value),
            arg("active", "bool", bool_value, "true|false"),
        ),
        note="Flag or clear the track's one active memory loop, then refresh cues "
        "and arm or disarm the matching safety slot, as the loop menu does.",
    ),
    # CUES-10013: rekordbox CALL < / >, `opendj memory_cue_call 1 next`.
    Verb(
        "memory_cue_call",
        "memory_cue_call",
        (
            _DECK,
            arg("direction", "enum", enum_value(("prev", "next")), "prev|next"),
        ),
    ),
    # ----- technically-working overlay and pairing ----------------------
    Verb("tech_mode_toggle", "tech_mode_toggle", ()),
    Verb("tech_mode_peek", "tech_mode_peek", (arg("peeking", "bool", bool_value, "true|false"),)),
    Verb("tech_mode_opt_reveal", "tech_mode_opt_reveal", (arg("revealed", "bool", bool_value, "true|false"),)),
    Verb("tech_mode_eq_raised", "tech_mode_eq_raised", (arg("raised", "bool", bool_value, "true|false"),)),
    Verb(
        "tech_mode_edge_hover",
        "tech_mode_edge_hover",
        (
            arg("edge", "enum", enum_value(EDGE_VALUES), "top|left|right|bottom"),
            arg("hovered", "bool", bool_value, "true|false"),
        ),
    ),
    # ----- top bar Track information (i) overlay (TOPVIEW-05) -----------
    Verb("track_info_toggle", "track_info_toggle", ()),
    Verb(
        "mixer_placement",
        "mixer_placement",
        (arg("placement", "enum", enum_value(MIXER_PLACEMENT_VALUES), "center|sides"),),
        note="Where the mixer sits: one center column or mirrored halves beside the decks "
        "(#9371); read back as ui.mixer_placement.",
    ),
    Verb("track_info_open", "track_info_open", (arg("stable_id", "text", text_value),)),
    Verb("track_info_close", "track_info_close", ()),
    Verb(
        "mixer_deck_pair_select",
        "mixer_deck_pair_select",
        (
            arg("pair", "enum", enum_value(MIXER_DECK_PAIR_VALUES), "1-3|2-4"),
            arg("deck", "deck", deck_value, "1-4, a deck in the pair", optional=True),
        ),
        note="FLX10-style deck pair select (#13228): with deck, select that deck of the pair; "
        "without, toggle between the pair's two decks. The other pair's selection is kept; "
        "read back as mixer_selection.selected.",
    ),
    Verb("oj_toggle", "oj_toggle", ()),
    Verb(
        "transition_preset",
        "transition_preset",
        (arg("preset", "enum", enum_value(TRANSITION_PRESET_VALUES), "|".join(TRANSITION_PRESET_VALUES)),),
        note="Run a transition preset across the master and the other playing deck, as its "
        "preset.run hotkey does; sending it again while it runs aborts it (#13717).",
    ),
    Verb(
        "hotkeys_overlay",
        "hotkeys_overlay",
        (arg("open", "bool", bool_value, "true|false"),),
        observes=(Observe(("hotkeys_overlay_open",), "open"),),
        note="#12272: open or close the hotkeys overlay, the one ? toggles; UI state only, transport untouched.",
    ),
    Verb("pairing_snapshot_open", "pairing_snapshot_open", ()),
    Verb("pairing_snapshot_remove_eq_adjuster", "pairing_snapshot_remove_eq_adjuster", (_DECK, _BAND)),
    Verb(
        "pairing_snapshot_save",
        "pairing_snapshot_save",
        (
            arg("from_deck", "deck", deck_value, "1-4"),
            arg("to_deck", "deck", deck_value, "1-4"),
        ),
    ),
    Verb(
        "cmdk_open",
        "cmdk_open",
        (arg("query", "text", text_value, optional=True),),
        note="Requires the Cmd K v2 bar (flag cmdk.v2) on /performance.",
    ),
    Verb("cmdk_close", "cmdk_close", (), note="Requires the Cmd K v2 bar (flag cmdk.v2) on /performance."),
    # ----- top-bar drawers (PERFDRAWER-12580), read back as drawers.<name>.open -----
    Verb("drawer_open", "drawer_open", (arg("drawer", "enum", enum_value(DRAWER_VALUES), "jobs"),)),
    Verb("drawer_close", "drawer_close", (arg("drawer", "enum", enum_value(DRAWER_VALUES), "jobs"),)),
    Verb(
        "cmdk_pick",
        "cmdk_pick",
        (
            arg("index", "int", int_value, "row (0 = first)"),
            arg(
                "id",
                "text",
                text_value,
                "row id exactly as query().cmdk reports it "
                "(a pair row is pair:<stable_id>:loose or pair:<stable_id>:aligned:<basis>)",
                optional=True,
            ),
            arg("deck", "deck", deck_value, "1-4", optional=True),
            arg("target", "deck", deck_value, "the target query().cmdk reported; refused if it moved", optional=True),
            arg("confirm", "bool", bool_value, "true|false", optional=True),
            arg(
                "vocals_only",
                "bool",
                bool_value,
                "true|false: Shift+Enter, the track loads and its vocals are soloed once the stems are ready",
                optional=True,
            ),
        ),
        note="CMDK-19: pick row `index` of the open Cmd K bar as Enter does (a track loads to `deck`, "
        "an aligned pair at its captured offset with the fader at 0); `confirm` is Cmd+Enter "
        "over a playing deck; `vocals_only` is Shift+Enter. Name optional fields to skip earlier ones, "
        "as in `deck=2 confirm=true`. Requires the Cmd K v2 bar on /performance, open.",
    ),
    Verb(
        "cmdk_mode",
        "cmdk_mode",
        (arg("mode", "enum", enum_value(("plain", "ai")), "plain|ai"),),
        note="CMDK-20: switch the open Cmd K bar between plain and AI search, as Tab does. "
        "Requires the Cmd K v2 bar on /performance, open, with flag cmdk.v2.ai on.",
    ),
    Verb("playlist_undo", "playlist_undo", (), note="Requires a mounted playlist history panel."),
    Verb("playlist_redo", "playlist_redo", (), note="Requires a mounted playlist history panel."),
    Verb(
        "playlist_history_jump",
        "playlist_history_jump",
        (arg("command_id", "text", text_value, "entry id from GET /api/v1/playlist-history"),),
        note="LIBUX-8626: undo that entry and every newer one, or redo a pending entry and every "
        "older one, as clicking it in the undo popover does. Requires a mounted playlist "
        "history panel.",
    ),
    Verb(
        "rescue_resume",
        "rescue_resume",
        (
            arg("decks", "rescue_decks", rescue_decks_value, "1:5000[,3:12000]"),
            arg("master_deck", "deck", deck_value, "1-4"),
        ),
        note="RESCUE-02: schedule every listed deck at position_ms together; RESCUE-06: master_deck "
        "(one of the listed decks) is master once they start.",
    ),
    Verb(
        "rescue_stop_all",
        "rescue_stop_all",
        (),
        note="RESCUE-02 Undo: stop every deck restored by rescue playback together.",
    ),
    Verb(
        "window",
        "window",
        (
            arg("action", "enum", enum_value(("open", "close", "focus")), "open|close|focus"),
            arg("label", "enum", enum_value(("second-screen",)), "second-screen"),
        ),
        note="Open, close or focus a labelled desktop-shell window. "
        "In a browser tab the page refuses with a reason that names the shell. "
        "query().windows and the UI mirror windows field list the labels that are open.",
    ),
)

VERBS: dict[str, Verb] = {verb.name: verb for verb in _VERBS}
VERB_NAMES: tuple[str, ...] = tuple(verb.name for verb in _VERBS)

# CLI-only (not an AGENT-03 bus command). `opendj midi monitor` listens on a
# MIDI port and prints each message with its exported-map action or "unmapped".
MIDI_MONITOR_USAGE = "opendj midi monitor --device <name-match> [--seconds N]"
MIDI_MONITOR_DEVICE_HELP = "substring match against a MIDI input port name"
MIDI_MONITOR_SECONDS_HELP = "stop after N seconds and print fader verdicts (default: until interrupt)"

# Re-exported so callers that only need "is this a deck?" do not import kinds.
__all__ = [
    "DECK_VALUES",
    "MIDI_MONITOR_DEVICE_HELP",
    "MIDI_MONITOR_SECONDS_HELP",
    "MIDI_MONITOR_USAGE",
    "NULL",
    "PRESENT",
    "RAMPABLE_TYPES",
    "VERBS",
    "VERB_NAMES",
    "Expectation",
    "Observe",
    "Verb",
]
