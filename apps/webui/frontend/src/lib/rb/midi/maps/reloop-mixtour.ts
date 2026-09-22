/**
 * Reloop Mixtour P0 device map (build unit: mixtour map).
 *
 * SOURCES (every wire number below is cited, none invented):
 *   [S4] Community-verified Mixxx mapping built from Reloop's official MIDI
 *        map: JackMcCrack, "mixxx---reloop-mixtour-settings",
 *        MIXTOUR_MIDI_1.midi.xml - spike source [4] in
 *        .planning/rekordbox-parity/spikes/SPIKE-CONTROLLERS.md.
 *        Wire convention there: status 0x90/0xB0/0xE0 = Note/CC/PitchBend on
 *        MIDI channel 1 (deck 1), status 0x91/0xB1/0xE1 = channel 2 (deck 2).
 *        This file uses the 1-based `ch` convention from midi-types.
 *   [SPIKE 1b] Mixtour hardware inventory (no jog wheels; Transport pad mode
 *        Play/Cue/Sync/Auto-Loop; Hot Cue pad mode with 4 cues).
 *   [SPIKE 2b] Mixtour transport is plain Note/CC, VU meter feedback is
 *        Note-On velocity = signal level (0x00-0x7F).
 *
 * MIXTOUR PRO: deliberately excluded by nameMatch. Its transport, load,
 * mixer, pad and global addresses differ and live in reloop-mixtour-pro.ts.
 *
 * Requirements (mini-PRD):
 *   ✔︎ 2-deck transport (play/cue), 4 hot cues per deck, beat-loop +
 *     loop-exit, trim/EQ/fader, crossfader, master level, pitch, browse
 *     encoder + load - all P0 actions, all cited to [S4].
 *     [if] a pad/knob listed in [S4] within P0 scope has no binding here
 *       [then ⛔️] map is incomplete
 *     [if] a binding here carries a Note/CC number absent from [S4]
 *       [then ⛔️] fabricated mapping, reject in review
 *   ✔︎ Hot-cue pad LED feedback via LedRule (binary 0x7F/0x00 per [S4]
 *     output rows).
 *     [if] hot cue A exists on deck 1 [then] note 0x0D ch1 lights 0x7F
 *   ✔︎ VU-over-MIDI output data exported for the P1 meter loop (velocity
 *     STEPS per [S4] output rows). LedTrigger has no signal-level selector
 *     (contract is P0), so these are typed constants, not LedRules.
 *     [if] a P1 VU loop needs addresses/levels [then] it imports
 *       MIXTOUR_VU_OUTPUTS / MIXTOUR_VU_VELOCITY_STEPS, no re-research
 *   → Sync (note 0x0A), filter/super1 (CC 0x04), kill switches, jog-helper
 *     script rows: outside the P0 MidiAction union (spike section 5 P0
 *     scope). Unbound on purpose; they learn-log. PFL (note 0x03) IS bound.
 */

import type { ControlHint, DeviceMap, LedRule, MidiBinding } from '$lib/rb/midi/midi-types';

// --------------------------------------------------------------- bindings

/** Per-deck rows. [S4] deck 1 = wire channel 1, deck 2 = wire channel 2,
 * with identical Note/CC ids for the mirrored controls listed here. */
function _deckBindings(ch: number, deck: 1 | 2): MidiBinding[] {
	return [
		// [S4] 0x90/0x91 0x0C -> play
		{ source: { ch, kind: 'note', id: 0x0c }, action: { type: 'deck_play_toggle', deck } },
		// [S4] 0x90/0x91 0x0B -> cue_default
		{ source: { ch, kind: 'note', id: 0x0b }, action: { type: 'deck_cue', deck } },
		// [S4] 0x90/0x91 0x03 -> PFL (channel cue / headphone pre-fader listen)
		{ source: { ch, kind: 'note', id: 0x03 }, action: { type: 'channel_cue', deck } },
		// [S4] 0x90/0x91 0x09 -> reloop_exit
		{ source: { ch, kind: 'note', id: 0x09 }, action: { type: 'deck_loop_exit', deck } },
		// [S4] 0x90/0x91 0x0D..0x10 -> hotcue_1..4_activate (Mixtour Hot Cue
		// pad mode has exactly 4 cues - spike 1b)
		{ source: { ch, kind: 'note', id: 0x0d }, action: { type: 'deck_hot_cue', deck, slot: 'A' } },
		{ source: { ch, kind: 'note', id: 0x0e }, action: { type: 'deck_hot_cue', deck, slot: 'B' } },
		{ source: { ch, kind: 'note', id: 0x0f }, action: { type: 'deck_hot_cue', deck, slot: 'C' } },
		{ source: { ch, kind: 'note', id: 0x10 }, action: { type: 'deck_hot_cue', deck, slot: 'D' } },
		// [S4] 0x90/0x91 0x02 -> LoadSelectedTrack per deck
		{ source: { ch, kind: 'note', id: 0x02 }, action: { type: 'browse_load', deck } },
		// [S4] 0xB0/0xB1 0x00 -> pregain (trim)
		{
			source: { ch, kind: 'cc', id: 0x00 },
			action: { type: 'mixer_channel', deck, target: 'trim' }
		},
		// [S4] EQ knobs -> EqualizerRack1 Effect1: CC 0x01 -> parameter3
		// (high), CC 0x02 -> parameter2 (mid), CC 0x03 -> parameter1 (low)
		{
			source: { ch, kind: 'cc', id: 0x01 },
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'high' }
		},
		{
			source: { ch, kind: 'cc', id: 0x02 },
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'mid' }
		},
		{
			source: { ch, kind: 'cc', id: 0x03 },
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'low' }
		},
		// [S4] 0xB0/0xB1 0x05 -> volume (channel fader)
		{
			source: { ch, kind: 'cc', id: 0x05 },
			action: { type: 'mixer_channel', deck, target: 'fader' }
		},
		// [S4] 0xE0/0xE1 -> rate: pitch fader arrives as a single 14-bit
		// Pitch Bend message, so no CC MSB/LSB pairing (lsbOffset null)
		{
			source: { ch, kind: 'pitchbend', id: 0 },
			action: { type: 'deck_pitch', deck, lsbOffset: null }
		}
	];
}

/** Global (deck-independent) rows. All on wire channel 1 per [S4]. */
const _GLOBAL_BINDINGS: MidiBinding[] = [
	// [S4] 0xB0 0x08 -> [Master] crossfader; [S4] also lists 0xB0 0x12 ->
	// [Master] crossfader as a second row (both bound - same action, so
	// whichever the firmware layer emits works)
	{ source: { ch: 1, kind: 'cc', id: 0x08 }, action: { type: 'mixer_global', target: 'crossfader' } },
	{ source: { ch: 1, kind: 'cc', id: 0x12 }, action: { type: 'mixer_global', target: 'crossfader' } },
	// [S4] 0xB0 0x0F -> [Master] volume
	{ source: { ch: 1, kind: 'cc', id: 0x0f }, action: { type: 'mixer_global', target: 'master' } },
	// [S4] 0xB0 0x07 -> [Playlist] SelectTrackKnob (selectknob = relative
	// encoder); [S4] also lists 0xB0 0x13 -> SelectNextTrack_or_prevTrack
	// (script relative browse) - both bound to the same action
	{ source: { ch: 1, kind: 'cc', id: 0x07 }, action: { type: 'browse_encoder' }, relative: true },
	{ source: { ch: 1, kind: 'cc', id: 0x13 }, action: { type: 'browse_encoder' }, relative: true },
	// [S4] 0x90 0x13 -> LoadSelectedTrack [Channel1] and 0x90 0x4F ->
	// LoadSelectedTrack [Channel2]: the alternate load-button layer sits on
	// wire channel 1 for BOTH decks (note range, not channel, selects deck)
	{ source: { ch: 1, kind: 'note', id: 0x13 }, action: { type: 'browse_load', deck: 1 } },
	{ source: { ch: 1, kind: 'note', id: 0x4f }, action: { type: 'browse_load', deck: 2 } },
	// [S4] 0x90 0x11 [Channel1] / 0x90 0x4D [Channel2] -> BeatLoop (same
	// channel-1 alternate layer as the load buttons above). The 4-beat
	// length is OUR software default for the Transport-mode Auto-Loop pad
	// (spike 1b names the pad, the wire carries no length)
	{ source: { ch: 1, kind: 'note', id: 0x11 }, action: { type: 'deck_beat_loop', deck: 1, beats: 4 } },
	{ source: { ch: 1, kind: 'note', id: 0x4d }, action: { type: 'deck_beat_loop', deck: 2, beats: 4 } }
];

// -------------------------------------------------------- best-guess hints

/** Documented-but-unbound controls ([S4], outside the P0 MidiAction union -
 * see the header's "→" scope note). Named per deck so unmapped traffic from
 * them reads as "likely: Sync (deck 1)" rather than a bare "unmapped source".
 * Only controls with a CITED wire number are hinted (kill switches and the
 * jog-helper script rows carry no [S4] number, so no hint - never invent). */
function _deckHints(ch: number, deck: 1 | 2): ControlHint[] {
	return [
		// [S4] 0x90/0x91 0x0A -> Sync (no sync action in P0 - spike 4/§5).
		{ source: { ch, kind: 'note', id: 0x0a }, label: `Sync (deck ${deck})` },
		// [S4] 0xB0/0xB1 0x04 -> filter / super1 (no filter action in P0).
		{ source: { ch, kind: 'cc', id: 0x04 }, label: `Filter / colour (deck ${deck})` }
	];
}

// ------------------------------------------------------------ led feedback

/** Hot-cue pad LEDs: [S4] output rows - Note On to the SAME (ch, note) as
 * the pad input, velocity 0x7F lit / 0x00 off (binary, no RGB palette on
 * this controller - spike 3b row 12). */
function _deckHotCueLeds(ch: number, deck: 1 | 2): LedRule[] {
	const slots = ['A', 'B', 'C', 'D'] as const;
	return slots.map((slot, i) => ({
		trigger: { kind: 'hot_cue_present', deck, slot },
		out: { ch, note: 0x0d + i, velocityOn: 0x7f, velocityOff: 0x00 }
	}));
}

// --------------------------------------------------------- vu meter (P1)

/** One Mixtour VU meter output address. [S4] output rows: master L = note
 * 0x12 ch1, master R = note 0x12 ch2, per-deck = note 0x11 ch1/ch2. */
export interface MixtourVuOutput {
	target: 'master_left' | 'master_right' | 'deck1' | 'deck2';
	ch: number;
	note: number;
}

/** VU-over-MIDI addresses for the P1 meter loop (spike section 5 puts the
 * active measure-and-send loop in P1; spike 2b documents the mechanism as
 * Note-On velocity = signal level). Exported now so P1 needs no re-research.
 * NOT LedRules: LedTrigger has no signal-level selector in the P0 contract. */
export const MIXTOUR_VU_OUTPUTS: MixtourVuOutput[] = [
	{ target: 'master_left', ch: 1, note: 0x12 },
	{ target: 'master_right', ch: 2, note: 0x12 },
	{ target: 'deck1', ch: 1, note: 0x11 },
	{ target: 'deck2', ch: 2, note: 0x11 }
];

/** Level -> velocity steps, verbatim from [S4] output rows (min01 is the
 * lower bound of each band; evaluate top-down, first match wins). */
export const MIXTOUR_VU_VELOCITY_STEPS: ReadonlyArray<{ min01: number; velocity: number }> = [
	{ min01: 0.98, velocity: 0x7f },
	{ min01: 0.85, velocity: 0x67 },
	{ min01: 0.65, velocity: 0x4d },
	{ min01: 0.4, velocity: 0x33 },
	{ min01: 0.01, velocity: 0x19 },
	{ min01: 0.0, velocity: 0x00 }
];

// -------------------------------------------------------------- device map

/** Reloop Mixtour classic P0 map. The negative lookahead is a safety boundary:
 * broad `Mixtour` matching used to route Pro SYNC as classic LOAD. */
export const RELOOP_MIXTOUR_MAP: DeviceMap = {
	vendor: 'Reloop',
	nameMatch: '\\bMixtour\\b(?!\\s+Pro\\b)',
	bindings: [..._deckBindings(1, 1), ..._deckBindings(2, 2), ..._GLOBAL_BINDINGS],
	leds: [..._deckHotCueLeds(1, 1), ..._deckHotCueLeds(2, 2)],
	hints: [..._deckHints(1, 1), ..._deckHints(2, 2)]
};
