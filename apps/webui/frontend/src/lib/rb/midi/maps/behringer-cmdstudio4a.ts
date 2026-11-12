/**
 * Behringer CMD Studio 4a device map (issue #10813, SPRINT400-S11).
 *
 * SOURCES (clean room: wires transcribed from the manufacturer chart only;
 * Mixxx GPL map used read-only afterward for cross-check, not as a source):
 *   [CHART] Behringer "CMD STUDIO 4a MIDI MAP", one page (raster; cite surface
 *        regions, not numbered sections).
 *        http://www.behringer.com/assets/CMD_STUDIO_4A_MAP.pdf (dead)
 *        https://web.archive.org/web/20150701200912id_/http://www.behringer.com:80/assets/CMD_STUDIO_4A_MAP.pdf
 *        sha256 6e7df9a2224bef2ebae98d5926f353ce4428721dd0325bc32599eefa6c2f70fd
 *
 * hardware_confirmed: false (no unit on hand; wires from [CHART] only).
 *
 * Wire convention: two physical sides, four deck channels. Left side deck A
 * = ch 1, deck C = ch 3 (same note/CC ids). Right side deck B = ch 2, deck D
 * = ch 4. Grey strip (browse, master, crossfader, headphones) is always ch 1.
 * Scientific pitch: MIDI = 12 * (octave + 1) + pitch class. `ch` is 1-based.
 * Grey silkscreen names are mostly illegible on the archived chart; control
 * roles follow legible wire labels and surface position.
 *
 * Requirements (mini-PRD):
 *   ✔︎ Transport, sync, 8 hot cues, manual loop pair, load, jog, pitch (bend),
 *     3-band EQ, PFL, fader, browse, master, crossfader, headphone mix/level.
 *     [if] PLAY deck A is not note ch1 44 [then ⛔️] wire drift from [CHART]
 *     [if] a binding (ch, kind, id) is absent from [CHART] [then ⛔️] fabricated
 *   ✔︎ FX knob 4 = mixer_channel filter (colour FX); other FX controls hinted.
 *     MidiAction has no beat-FX action (contract change outside #10813).
 *   ✔︎ Pitch fader = Pitch Bend id 0, lsbOffset null; PITCH BEND +/- buttons hinted.
 *   ✔︎ Jog and browse encoders: relativeCentered (encoding unverified until hardware).
 *   → Peak meters CC126/127 range 48-63: MidiMeterOutput has no offset, unbound.
 *   ✔︎ LED feedback: velocityOn 0x01, velocityOff 0x00 per chart.
 *   ✔︎ No shift bindings; deck select switches channels in hardware.
 */

import type {
	ControlHint,
	DeviceMap,
	DeviceMapEntry,
	LedRule,
	MidiBinding
} from '$lib/rb/midi/midi-types';
import type { DeckId } from '$lib/rb/deck-slots';

export const BEHRINGER_CMD_STUDIO_4A_HARDWARE_CONFIRMED = false as const;

/** Left physical side wire ids ([CHART] blue area). */
const _LEFT = {
	// [CHART] left transport row
	cue: 43, // G2
	play: 44, // G#2
	sync: 45, // A2
	hotCuesTop: [34, 35, 36, 37], // A#1..C#2
	hotCuesBottom: [38, 39, 40, 41], // D2..F2
	hotCueFifth: 42, // F#2
	loop: [23, 24], // B0, C1
	loopLarge: 25, // C#1
	load: 80, // G#5
	fxAssign: [82, 83], // A#5, B5
	fxKnobs: [16, 17, 18], // CC016-018
	fxKnob4: 19, // CC019
	fxButtons: [16, 17, 18, 19], // E0..G0
	roundJog: 22, // A#0
	abovePitch: 27, // D#1
	jogCc: 26, // CC026 TURN
	jogTouch: 26, // D1 TOUCH
	pitchBendBtns: [32, 33], // G#1, A1
	eq: [96, 97, 98], // CC096-098
	eqButtons: [96, 97, 98], // C7, C#7, D7
	pfl: 106, // A#7
	fader: 112 // CC112
} as const;

/** Right physical side wire ids ([CHART] orange area). */
const _RIGHT = {
	cue: 75, // D#5
	play: 76, // E5
	sync: 77, // F5
	hotCuesTop: [66, 67, 68, 69], // F#4..A4
	hotCuesBottom: [70, 71, 72, 73], // A#4..C#5
	hotCueFifth: 74, // D5
	loop: [56, 55], // G#3, G3
	loopLarge: 57, // A3
	load: 81, // A5
	fxAssign: [84, 85], // C6, C#6
	fxKnobs: [48, 49, 50], // CC048-050
	fxKnob4: 51, // CC051
	fxButtons: [48, 49, 50, 51], // C3..D#3
	roundJog: 54, // F#3
	abovePitch: 59, // B3
	jogCc: 58, // CC058 TURN
	jogTouch: 58, // A#3 TOUCH
	pitchBendBtns: [64, 65], // E4, F4
	eq: [99, 100, 101], // CC099-101
	eqButtons: [99, 100, 101], // D#7, E7, F7
	pfl: 107, // B7
	fader: 113 // CC113
} as const;

type Side = typeof _LEFT | typeof _RIGHT;

const _DECK_LETTER: Record<DeckId, string> = { 1: 'A', 2: 'B', 3: 'C', 4: 'D' };

// --------------------------------------------------------------- bindings

function _deckBindings(ch: number, deck: DeckId, side: Side): MidiBinding[] {
	const hotSlots = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'] as const;
	const hotNotes = [...side.hotCuesTop, ...side.hotCuesBottom];
	return [
		{ source: { ch, kind: 'note', id: side.cue }, action: { type: 'deck_cue', deck } },
		{ source: { ch, kind: 'note', id: side.play }, action: { type: 'deck_play_toggle', deck } },
		{ source: { ch, kind: 'note', id: side.sync }, action: { type: 'deck_sync_toggle', deck } },
		...side.loop.map((id): MidiBinding => ({
			source: { ch, kind: 'note', id },
			action: { type: 'deck_manual_loop_cycle', deck }
		})),
		{ source: { ch, kind: 'note', id: side.load }, action: { type: 'browse_load', deck } },
		...hotNotes.map((id, i): MidiBinding => ({
			source: { ch, kind: 'note', id },
			action: { type: 'deck_hot_cue', deck, slot: hotSlots[i] }
		})),
		{ source: { ch, kind: 'note', id: side.pfl }, action: { type: 'channel_cue', deck } },
		{
			source: { ch, kind: 'cc', id: side.fader },
			action: { type: 'mixer_channel', deck, target: 'fader' }
		},
		{
			source: { ch, kind: 'cc', id: side.eq[0] },
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'high' }
		},
		{
			source: { ch, kind: 'cc', id: side.eq[1] },
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'mid' }
		},
		{
			source: { ch, kind: 'cc', id: side.eq[2] },
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'low' }
		},
		{
			source: { ch, kind: 'cc', id: side.fxKnob4 },
			action: { type: 'mixer_channel', deck, target: 'filter' }
		},
		{
			source: { ch, kind: 'pitchbend', id: 0 },
			action: { type: 'deck_pitch', deck, lsbOffset: null }
		},
		{
			source: { ch, kind: 'cc', id: side.jogCc },
			action: { type: 'deck_jog', deck },
			relative: true,
			relativeCentered: true
		}
	];
}

const _GLOBAL_BINDINGS: MidiBinding[] = [
	{
		source: { ch: 1, kind: 'cc', id: 1 },
		action: { type: 'browse_encoder' },
		relative: true,
		relativeCentered: true,
		relativeUnit: true
	},
	{ source: { ch: 1, kind: 'cc', id: 47 }, action: { type: 'mixer_global', target: 'master' } },
	{
		source: { ch: 1, kind: 'cc', id: 114 },
		action: { type: 'mixer_global', target: 'crossfader' }
	},
	{ source: { ch: 1, kind: 'cc', id: 122 }, action: { type: 'headphone_mix' } },
	{ source: { ch: 1, kind: 'cc', id: 123 }, action: { type: 'headphone_level' } }
];

// -------------------------------------------------------- documented hints

function _deckHints(ch: number, deck: DeckId, side: Side): ControlHint[] {
	const d = `deck ${_DECK_LETTER[deck]}`;
	return [
		...side.fxKnobs.map((id, i): ControlHint => ({
			source: { ch, kind: 'cc', id },
			label: `FX row knob ${i + 1} (${d}); MidiAction has no beat-FX action (contract change outside #10813)`
		})),
		...side.fxButtons.map((id, i): ControlHint => ({
			source: { ch, kind: 'note', id },
			label: `FX row button ${i + 1} (${d}); MidiAction has no beat-FX action (contract change outside #10813)`
		})),
		...side.fxAssign.map((id, i): ControlHint => ({
			source: { ch, kind: 'note', id },
			label: `FX ASSIGN ${i + 1} (${d}); MidiAction has no beat-FX action (contract change outside #10813)`
		})),
		{ source: { ch, kind: 'note', id: side.loopLarge }, label: `LOOP large button (${d})` },
		{
			source: { ch, kind: 'note', id: side.roundJog },
			label: `Round button upper-left of jog (${d})`
		},
		{ source: { ch, kind: 'note', id: side.abovePitch }, label: `Button above pitch fader (${d})` },
		{ source: { ch, kind: 'note', id: side.jogTouch }, label: `Jog TOUCH (${d})` },
		...side.pitchBendBtns.map((id, i): ControlHint => ({
			source: { ch, kind: 'note', id },
			label: `PITCH BEND ${i === 0 ? '-' : '+'} button (${d})`
		})),
		{ source: { ch, kind: 'note', id: side.hotCueFifth }, label: `Fifth HOT CUE button (${d})` },
		...side.eqButtons.map((id, i): ControlHint => ({
			source: { ch, kind: 'note', id },
			label: `EQ button under knob ${i + 1} (${d})`
		}))
	];
}

const _GLOBAL_HINTS: ControlHint[] = [
	{ source: { ch: 1, kind: 'note', id: 1 }, label: 'Button under BROWSE encoder' },
	{ source: { ch: 1, kind: 'note', id: 2 }, label: 'Button beside BROWSE encoder (D-1)' },
	{ source: { ch: 1, kind: 'note', id: 3 }, label: 'Button beside BROWSE encoder (Eb-1)' }
];

// ------------------------------------------------------------ led feedback

function _deckLeds(ch: number, deck: DeckId, side: Side): LedRule[] {
	const led = (note: number) => ({ ch, note, velocityOn: 0x01, velocityOff: 0x00 });
	const hotSlots = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'] as const;
	const hotNotes = [...side.hotCuesTop, ...side.hotCuesBottom];
	return [
		{ trigger: { kind: 'deck_playing', deck }, out: led(side.play) },
		{ trigger: { kind: 'beat_sync_enabled', deck }, out: led(side.sync) },
		{ trigger: { kind: 'channel_cue_enabled', deck }, out: led(side.pfl) },
		...side.loop.map((id): LedRule => ({
			trigger: { kind: 'loop_engaged', deck },
			out: led(id)
		})),
		...hotNotes.map((id, i): LedRule => ({
			trigger: { kind: 'hot_cue_present', deck, slot: hotSlots[i] },
			out: led(id)
		}))
	];
}

// -------------------------------------------------------------- device map

export const BEHRINGER_CMD_STUDIO_4A_MAP: DeviceMap = {
	vendor: 'Behringer',
	nameMatch: '\\bCMD\\s*Studio\\s*4a\\b',
	bindings: [
		..._deckBindings(1, 1, _LEFT),
		..._deckBindings(3, 3, _LEFT),
		..._deckBindings(2, 2, _RIGHT),
		..._deckBindings(4, 4, _RIGHT),
		..._GLOBAL_BINDINGS
	],
	leds: [
		..._deckLeds(1, 1, _LEFT),
		..._deckLeds(3, 3, _LEFT),
		..._deckLeds(2, 2, _RIGHT),
		..._deckLeds(4, 4, _RIGHT)
	],
	hints: [
		..._deckHints(1, 1, _LEFT),
		..._deckHints(3, 3, _LEFT),
		..._deckHints(2, 2, _RIGHT),
		..._deckHints(4, 4, _RIGHT),
		..._GLOBAL_HINTS
	]
};

/** Self-registration: maps/entries.ts discovers this export. */
export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{ key: 'BEHRINGER_CMD_STUDIO_4A_MAP', map: BEHRINGER_CMD_STUDIO_4A_MAP, live: true }
];
