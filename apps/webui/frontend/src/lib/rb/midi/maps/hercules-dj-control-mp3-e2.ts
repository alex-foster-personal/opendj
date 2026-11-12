/**
 * Hercules DJ Control MP3 e2 device map (issue #10840).
 *
 * SOURCE OF TRUTH for every Note/CC in this file: the manufacturer User Manual,
 * section 6 "CONFIGURATION IN MIDI MODE", pages 22-29.
 * URL: https://ts.hercules.com/download/sound/manuals/MP3_E2/01_DJ_Control_MP3_e2_ENG.pdf
 * sha256: ec5a4a109087082d25a516ff3154fc966f545f5c67a4ae7097726697bca73e4a
 *
 * DO NOT source numbers from tools/deck-diagrams/devices/hercules-dj-control-mp3-e2/midi.json
 * (Mixxx-derived) or from Mixxx (GPL). Do not copy wires from sibling maps.
 *
 * The chart has no MIDI channel column; every wire uses ch 1. A control-panel
 * channel change (manual 5.2.3) breaks name/port matching.
 *
 * hardware_confirmed: false -- built from the chart only, not exercised on hardware.
 *
 * Wire convention: Note On velocity 127 = press, 0 = release; jog CCs 48/49 and pitch
 * CCs 50/51 are two's-complement increments; analog CCs are 0..127; EQ knobs 0 = full CW.
 *
 * ✔︎ [if] any binding's (ch, kind, id) is not in section 6 [then ⛔️] reject at test time.
 * ✔︎ [if] nameMatch does not fit the catalog USB name [then ⛔️]
 * ✔︎ [if] play, cue, sync, both faders, crossfader or jog are missing [then ⛔️]
 * → FX buttons (SHIFTED_K1-K4): no beat-FX or colour-FX MidiAction (#9788); hinted only.
 * → PITCH_CC 50/51: relative increments; deck_pitch needs continuous input; hinted only.
 * → deck_beat_loop: a second press restarts the loop; the manual says resume (runtime limit).
 */

import type {
	ControlHint,
	DeviceMap,
	DeviceMapEntry,
	LedRule,
	MidiBinding
} from '$lib/rb/midi/midi-types';

const CH = 1;
const LED_ON = 0x7f;
const LED_OFF = 0x00;

/** Manual 5.6: buttons 1-4 are loops of 1/2/4/8 measures (4/8/16/32 beats). */
const LOOP_BEATS = [4, 8, 16, 32] as const;

type DeckUnit = 'A' | 'B';

const DECK_NOTES: Record<
	DeckUnit,
	{
		deck: 1 | 2;
		k: readonly [number, number, number, number];
		play: number;
		cue: number;
		sync: number;
		listen: number;
		load: number;
		pbm: number;
		pbp: number;
		jogCc: number;
		volCc: number;
		trebleCc: number;
		medCc: number;
		bassCc: number;
		shiftedK: readonly [number, number, number, number];
		shift: number;
		shiftState: number;
		pitchCc: number;
		mtempo: number;
		prev: number;
		next: number;
	}
> = {
	A: {
		deck: 1,
		k: [1, 2, 3, 4],
		play: 15,
		cue: 14,
		sync: 18,
		listen: 16,
		load: 17,
		pbm: 10,
		pbp: 11,
		jogCc: 48,
		volCc: 52,
		trebleCc: 53,
		medCc: 54,
		bassCc: 55,
		shiftedK: [5, 6, 7, 8],
		shift: 9,
		shiftState: 20,
		pitchCc: 50,
		mtempo: 19,
		prev: 12,
		next: 13
	},
	B: {
		deck: 2,
		k: [21, 22, 23, 24],
		play: 35,
		cue: 34,
		sync: 38,
		listen: 36,
		load: 37,
		pbm: 30,
		pbp: 31,
		jogCc: 49,
		volCc: 57,
		trebleCc: 58,
		medCc: 59,
		bassCc: 60,
		shiftedK: [25, 26, 27, 28],
		shift: 29,
		shiftState: 40,
		pitchCc: 51,
		mtempo: 39,
		prev: 32,
		next: 33
	}
};

function _note(id: number) {
	return { ch: CH, kind: 'note' as const, id };
}

function _cc(id: number) {
	return { ch: CH, kind: 'cc' as const, id };
}

function _deckBindings(unit: DeckUnit): MidiBinding[] {
	const d = DECK_NOTES[unit];
	const deck = d.deck;
	const out: MidiBinding[] = [
		{ source: _note(d.play), action: { type: 'deck_play_toggle', deck } },
		{ source: _note(d.cue), action: { type: 'deck_cue', deck } },
		{ source: _note(d.sync), action: { type: 'deck_sync_toggle', deck } },
		{ source: _note(d.listen), action: { type: 'channel_cue', deck } },
		{ source: _note(d.load), action: { type: 'browse_load', deck } },
		{
			source: _note(d.pbm),
			action: { type: 'deck_tempo_nudge', deck, direction: -1 }
		},
		{
			source: _note(d.pbp),
			action: { type: 'deck_tempo_nudge', deck, direction: 1 }
		},
		{
			source: _cc(d.jogCc),
			action: { type: 'deck_jog', deck },
			relative: true
		},
		{ source: _cc(d.volCc), action: { type: 'mixer_channel', deck, target: 'fader' } }
	];
	// Chart: 0 = full CW, 127 = full CCW; engine 0..1 is cut-to-boost without invert.
	for (const [band, cc] of [
		['high', d.trebleCc],
		['mid', d.medCc],
		['low', d.bassCc]
	] as const) {
		out.push({
			source: _cc(cc),
			action: { type: 'mixer_channel', deck, target: 'eq', band },
			invert: true
		});
	}
	LOOP_BEATS.forEach((beats, i) => {
		out.push({
			source: _note(d.k[i]),
			action: { type: 'deck_beat_loop', deck, beats }
		});
	});
	return out;
}

const CROSSFADER: MidiBinding = {
	source: _cc(56),
	action: { type: 'mixer_global', target: 'crossfader' }
};

function _deckHints(unit: DeckUnit): ControlHint[] {
	const d = DECK_NOTES[unit];
	const deck = d.deck;
	return [
		{
			source: _note(d.shiftedK[0]),
			label: `SHIFTED_K1 effects button (deck ${deck}; manual 5.8; no beat/colour FX MidiAction, #9788)`
		},
		{
			source: _note(d.shiftedK[1]),
			label: `SHIFTED_K2 effects button (deck ${deck}; manual 5.8; #9788)`
		},
		{
			source: _note(d.shiftedK[2]),
			label: `SHIFTED_K3 effects button (deck ${deck}; manual 5.8; #9788)`
		},
		{
			source: _note(d.shiftedK[3]),
			label: `SHIFTED_K4 effects button (deck ${deck}; manual 5.8; #9788)`
		},
		{
			source: _note(d.shift),
			label: `Loops/Fx toggle (deck ${deck}; latched layer, manual 4.1 item 4)`
		},
		{
			source: _note(d.shiftState),
			label: `Shift_State (deck ${deck}; latch state report)`
		},
		{
			source: _cc(d.pitchCc),
			label: `PITCH jog (deck ${deck}; relative CC; no relative deck_pitch action)`
		},
		{
			source: _note(d.mtempo),
			label: `MTempo (deck ${deck}; master tempo; no action)`
		},
		{ source: _note(d.prev), label: `Prev track (deck ${deck}; no skip action)` },
		{ source: _note(d.next), label: `Next track (deck ${deck}; no skip action)` }
	];
}

const GLOBAL_HINTS: ControlHint[] = [
	{ source: _note(41), label: 'Menu_U (no browse step action)' },
	{ source: _note(42), label: 'Menu_D (no browse step action)' },
	{ source: _note(43), label: 'Files (no action)' },
	{ source: _note(44), label: 'Folders (no action)' },
	{ source: _note(45), label: 'Scratch (no action)' },
	{ source: _note(46), label: 'Automix (no action)' }
];

function _deckLeds(unit: DeckUnit): LedRule[] {
	const d = DECK_NOTES[unit];
	const deck = d.deck;
	return [
		{
			trigger: { kind: 'deck_playing', deck },
			out: { ch: CH, note: d.play, velocityOn: LED_ON, velocityOff: LED_OFF }
		},
		// Software choice: CUE LED follows loaded, not cue-active.
		{
			trigger: { kind: 'deck_loaded', deck },
			out: { ch: CH, note: d.cue, velocityOn: LED_ON, velocityOff: LED_OFF }
		},
		{
			trigger: { kind: 'channel_cue_enabled', deck },
			out: { ch: CH, note: d.listen, velocityOn: LED_ON, velocityOff: LED_OFF }
		},
		{
			trigger: { kind: 'beat_sync_enabled', deck },
			out: { ch: CH, note: d.sync, velocityOn: LED_ON, velocityOff: LED_OFF }
		},
		...LOOP_BEATS.map((beats, i): LedRule => ({
			trigger: { kind: 'loop_beats_engaged', deck, beats },
			out: { ch: CH, note: d.k[i], velocityOn: LED_ON, velocityOff: LED_OFF }
		}))
	];
}

export const HERCULES_MP3_E2_HARDWARE_CONFIRMED = false as const;

export const HERCULES_MP3_E2_MAP: DeviceMap = {
	vendor: 'Hercules',
	nameMatch: 'DJ\\s*Control\\s*MP3\\s*e2',
	bindings: [..._deckBindings('A'), ..._deckBindings('B'), CROSSFADER],
	leds: [..._deckLeds('A'), ..._deckLeds('B')],
	hints: [..._deckHints('A'), ..._deckHints('B'), ...GLOBAL_HINTS]
};

/** Self-registration: maps/entries.ts discovers this export, so no shared list names this map. */
export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{ key: 'HERCULES_MP3_E2_MAP', map: HERCULES_MP3_E2_MAP, live: true }
];
