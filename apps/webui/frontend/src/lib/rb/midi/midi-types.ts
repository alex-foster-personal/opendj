/**
 * MIDI mapping contract for the P0 controller build (build unit: midi core).
 *
 * THIS FILE IS THE CONTRACT between the midi units (webmidi runtime,
 * action glue, per-device maps, midi UI). Device-map units import these
 * types; the runtime consumes DeviceMap instances registered at startup.
 *
 * Grounding: .planning/rekordbox-parity/spikes/SPIKE-CONTROLLERS.md
 * (section 2: both target controllers - Pioneer DDJ-FLX10 per the official
 * AlphaTheta MIDI message list [spike source 1], Reloop Mixtour per the
 * community-verified Mixxx map [spike source 4] - speak plain Note/CC with
 * no SysEx; pad RGB LEDs are Note-On velocity palette writes; tempo faders
 * are standard 14-bit MSB/LSB CC pairs).
 *
 * P0 scope covered by MidiAction (spike section 5, "P0 plug-and-play
 * wins"): transport play/cue, hot cues, auto/beat loops, mixer continuous
 * controls (trim/EQ/fader/crossfader/master), pitch faders, browse+load,
 * shift-layer modifier. Scratch, jog bitmap, stems, sampler, FX and DVS are
 * explicitly NOT modelled here.
 *
 * Requirements (mini-PRD):
 *   ✔︎ Discriminated MidiAction union covering exactly the P0 scope.
 *     [if] a device map binds an action outside this union [then ⛔️] it
 *       fails to typecheck - the union is the scope fence
 *   ✔︎ DeviceMap carries nameMatch as a RegExp SOURCE string (serialisable,
 *     rebuilt at resolution time).
 *   ✔︎ LedRule triggers are state selectors, not callbacks (device maps
 *     stay pure data).
 *   ✔︎ LearnLogEntry captures raw bytes + decode + mapped flag so unmapped
 *     traffic is never silently dropped.
 */

import type { DeckId, EqBand, HotCueSlot } from '$lib/rb/types';

// ------------------------------------------------------------- midi source

/** Message families the P0 runtime decodes. Anything else that arrives on
 * the wire is learn-logged as unmapped, never silently dropped. */
export type MidiSourceKind = 'note' | 'cc' | 'pitchbend';

/** One physical control's wire identity.
 * ch: MIDI channel 1..16 (human convention, NOT the 0-based wire nibble).
 * id: note number for 'note', controller number for 'cc'; MUST be 0 for
 * 'pitchbend' (the message carries no id byte). */
export interface MidiSource {
	ch: number;
	kind: MidiSourceKind;
	id: number;
}

// ------------------------------------------------------------ midi actions

/** Continuous mixer targets scoped to one channel strip. */
export type MixerChannelTarget = 'trim' | 'eq' | 'fader';

/** Transport commands (spike rows FLX10 1-2 / Mixtour 7: EASY, near-1:1
 * onto engine play/pause/pressCue). */
export interface DeckPlayToggleAction {
	type: 'deck_play_toggle';
	deck: DeckId;
}
export interface DeckCueAction {
	type: 'deck_cue';
	deck: DeckId;
}

/** Hot-cue pad press (spike row FLX10 9: pad -> cueJump(hotcue.in_ms)). */
export interface DeckHotCueAction {
	type: 'deck_hot_cue';
	deck: DeckId;
	slot: HotCueSlot;
}

/** Auto/beat-loop pad (spike row FLX10 12: engageBeatLoop direct call). */
export interface DeckBeatLoopAction {
	type: 'deck_beat_loop';
	deck: DeckId;
	beats: number;
}

/** Loop exit (reloop/exit button family - disengages the active loop). */
export interface DeckLoopExitAction {
	type: 'deck_loop_exit';
	deck: DeckId;
}

/** Per-channel continuous mixer control (spike rows 17-19: setTrim/setEq/
 * setFader). EQ carries its band; other targets MUST NOT. */
export interface MixerChannelAction {
	type: 'mixer_channel';
	deck: DeckId;
	target: MixerChannelTarget;
	/** Required when target === 'eq', forbidden otherwise (validated at
	 * dispatch, fail-fast). */
	band?: EqBand;
}

/** Global continuous mixer controls (spike row 20 + engine setMaster). */
export interface MixerGlobalAction {
	type: 'mixer_global';
	target: 'crossfader' | 'master';
}

/** Pitch/tempo fader (spike row FLX10 7: standard 14-bit MSB/LSB CC pair
 * per the official MIDI list). Bind this action to the MSB CC source; the
 * runtime pairs the LSB CC (source.id + lsbOffset) into one 14-bit value.
 * Set lsbOffset to null for plain 7-bit pitch faders. */
export interface DeckPitchAction {
	type: 'deck_pitch';
	deck: DeckId;
	/** LSB controller offset from the MSB controller id; the MIDI-standard
	 * pairing is +32. null = 7-bit single-CC fader. */
	lsbOffset: number | null;
}

/** Browse rotary encoder: relative selection movement in the track list.
 * Encoders send relative ticks (spike 2a: CW 0x01..0x1E, CCW 0x7F..0x62,
 * i.e. two's-complement signed deltas). */
export interface BrowseEncoderAction {
	type: 'browse_encoder';
}

/** LOAD button: load the browser's selected track onto a deck. */
export interface BrowseLoadAction {
	type: 'browse_load';
	deck: DeckId;
}

/** Shift-layer modifier button. While held, bindings with shift: true win
 * over their unshifted twins on the same source. */
export interface ShiftModifierAction {
	type: 'shift_modifier';
}

/** Every action the P0 runtime can emit. Adding a case = widening P0
 * scope; do that in a contract change, not ad hoc in a device map. */
export type MidiAction =
	| DeckPlayToggleAction
	| DeckCueAction
	| DeckHotCueAction
	| DeckBeatLoopAction
	| DeckLoopExitAction
	| MixerChannelAction
	| MixerGlobalAction
	| DeckPitchAction
	| BrowseEncoderAction
	| BrowseLoadAction
	| ShiftModifierAction;

// ------------------------------------------------------------ input values

/** Normalised payload the dispatcher hands to the action handler alongside
 * the matched MidiAction. */
export type MidiInputValue =
	/** Note press/release (Note On velocity 0 normalises to release). */
	| { kind: 'button'; pressed: boolean; velocity: number }
	/** Absolute 7-bit CC normalised to 0..1. */
	| { kind: 'continuous'; value01: number; raw: number }
	/** Paired 14-bit MSB/LSB CC normalised to 0..1 (raw 0..16383). */
	| { kind: 'continuous14'; value01: number; raw: number }
	/** Relative encoder ticks decoded as signed two's-complement delta. */
	| { kind: 'relative'; delta: number };

// ---------------------------------------------------------------- bindings

/** One wire source -> one action. */
export interface MidiBinding {
	source: MidiSource;
	action: MidiAction;
	/** true = only matches while the shift layer is held. Unshifted
	 * bindings also fire while shift is held IF no shifted twin exists. */
	shift?: boolean;
	/** Invert a continuous value (1 - value01) for faders whose wire
	 * orientation opposes the engine's 0..1 convention. */
	invert?: boolean;
	/** Treat this CC as a relative encoder (two's-complement delta) instead
	 * of an absolute 0..127 value. Required true for browse_encoder. */
	relative?: boolean;
}

// ------------------------------------------------------------ led feedback

/** State selectors an LedRule can watch. Evaluated against the engine's
 * reactive deck stores; pure data so device maps stay declarative. */
export type LedTrigger =
	| { kind: 'deck_playing'; deck: DeckId }
	| { kind: 'deck_loaded'; deck: DeckId }
	| { kind: 'loop_engaged'; deck: DeckId }
	/** Lit when the hot-cue slot is populated. On RGB pads velocityOn may
	 * be overridden per-cue by the palette resolver (FLX10: Note-On
	 * velocity 1-127 selects the colour palette entry - spike 2a). */
	| { kind: 'hot_cue_present'; deck: DeckId; slot: HotCueSlot };

/** One LED output rule: when trigger is true send velocityOn, else
 * velocityOff, as a Note On to (ch, note) on the device's MIDI output. */
export interface LedRule {
	trigger: LedTrigger;
	out: {
		ch: number;
		note: number;
		velocityOn: number;
		velocityOff: number;
	};
}

// --------------------------------------------------------------- device map

/** Full declarative map for one controller model. */
export interface DeviceMap {
	vendor: string;
	/** RegExp SOURCE string matched (case-insensitive) against the WebMIDI
	 * port name, e.g. 'DDJ-FLX10'. Stored as a string so maps stay
	 * serialisable. */
	nameMatch: string;
	bindings: MidiBinding[];
	leds?: LedRule[];
}

// ---------------------------------------------------------------- learn log

/** One captured inbound message for the mapping-debug learn log. Unmapped
 * traffic MUST land here - that is the debugging tool for writing maps. */
export interface LearnLogEntry {
	/** performance.now() capture time. */
	ts: number;
	deviceId: string;
	deviceName: string;
	/** Raw wire bytes. */
	status: number;
	data1: number;
	data2: number;
	/** Decoded source identity; null when the status family is one the P0
	 * runtime does not decode at all. */
	decoded: MidiSource | null;
	/** True when a binding consumed this message. */
	mapped: boolean;
	/** Human-readable dispatch note ('unmapped', 'no handler', action type,
	 * error text...). Never empty for mapped === false. */
	note: string;
}
