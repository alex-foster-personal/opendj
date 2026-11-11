/**
 * Hercules DJ Control Instinct live device map (issue #10839).
 *
 * SOURCE OF TRUTH for every Note/CC number: the manufacturer's technical
 * specifications MIDI table, "MIDI commands -> MIDI input" (Note/CC numbers,
 * command type, value ranges) and "MIDI Output" (LED Note On/Off, inverted
 * velocities):
 *   https://ts.hercules.com/download/sound/manuals/DJ_Instinct/QSG/DJCInstinct_Technical_specifications.pdf
 * (not redistributed here). This is the original DJ Control Instinct /
 * Instinct S chart. Instinct P8 is a different SKU with a different command
 * list (jog Bx 30); nothing here is taken from that PDF.
 *
 * CLEAN ROOM: Mixxx `Hercules DJ Control Instinct.midi.xml` is GPL; this
 * repo is Apache 2.0. `tools/deck-diagrams/devices/hercules-dj-control-instinct/midi.json`
 * is marked `"mixxx_gpl": true`. Neither is a source of wires. Mixxx may be
 * opened only to cross-check a number already transcribed from the chart.
 *
 * hardware_confirmed: false. Manufacturer chart, not hardware-sniffed.
 *
 * Channel layout: the input table prints a Midi Command decimal and a type
 * (Note On/Off or Control Change). There is no MIDI-channel column. Deck A
 * and deck B share one address space (different command numbers, not
 * different channels). Every source binds on channel 1 (1-based `ch`).
 *
 * Buttons: velocity 127 pressed, 0 released. Incremental jog:
 *   127 > 64 : CCW Slow > Fast; 01 > 63 : CW Slow > Fast.
 * Analog faders/EQ: 0 > 127 full down/left/CCW to full up/right/CW.
 * Pitch faders are incremental (127 pitch down, 01 pitch up), not 7-bit
 * absolute, so they are hinted rather than bound as `deck_pitch`.
 *
 * MIDI-OUT LEDs use the same note numbers as input, with inverted
 * velocities: "127" : OFF, "0" : ON. Pioneer maps use 0x7F on / 0x00 off;
 * Instinct is the opposite (velocityOn 0x00, velocityOff 0x7F).
 *
 * Chart defect: LOOP_KP4_DB is not printed. Page 3 and the MIDI-OUT table
 * label note 38 as CUE_KP4_DB, and page 4 also prints CUE_KP4_DB as note 42
 * (with CUE_KP1-3_DB = 39-41). Hot cues for deck 2 bind notes 39-42. Note 38
 * is hinted as the printed duplicate. LOOP_KP4_DB is not invented.
 *
 * PLAY_DB is note 48 and JOG_DA is CC 48 — different `kind`, so both bind.
 * No SHIFT row on this SKU (P8 has one). No deck 3/4 bindings.
 *
 * Requirements (mini-PRD):
 *   [if] play, cue, sync, both channel faders, the crossfader or either
 *     deck's jog is unbound [then ⛔️]
 *   [if] an FX_KP pad note from the chart arrives [then] color_fx for that
 *     deck (filter, space, echo, or next) ⛔️
 *   [if] a binding's (channel, kind, id) is not in the manufacturer chart
 *     [then ⛔️]
 *   [if] nameMatch matches Instinct P8 [then ⛔️]
 *
 * Hinted (out of a 1:1 action, or incremental-vs-absolute): pitch CCs,
 * sample pads, jog-touch, scratch, browse up/down, files/folders, HP vol,
 * PREV/NEXT, printed duplicate note 38.
 */

import type {
	ControlHint,
	DeviceMap,
	DeviceMapEntry,
	LedRule,
	MidiBinding,
	MidiSource
} from '$lib/rb/midi/midi-types';
import type { HotCueSlot } from '$lib/rb/hot-cue-types';

const CH = 1;
/** [MIDI Output] "127" : OFF – "0" : ON. */
const INSTINCT_LED_ON = 0x00;
const INSTINCT_LED_OFF = 0x7f;

const HOT_CUE_SLOTS: readonly HotCueSlot[] = ['A', 'B', 'C', 'D'];
const COLOR_FX_EFFECTS = ['filter', 'space', 'echo', 'next'] as const;

const note = (id: number): MidiSource => ({ ch: CH, kind: 'note', id });
const cc = (id: number): MidiSource => ({ ch: CH, kind: 'cc', id });

type Deck = 1 | 2;

type DeckSpec = {
	deck: Deck;
	play: number;
	cue: number;
	sync: number;
	listen: number;
	load: number;
	jog: number;
	vol: number;
	treble: number;
	medium: number;
	bass: number;
	cues: readonly [number, number, number, number];
	loops: readonly { note: number; beats: number }[];
	fx: readonly [number, number, number, number];
	pbm: number;
	pbp: number;
};

/** Printed MIDI input rows, decimal, deck A then deck B. */
const DECKS: readonly DeckSpec[] = [
	{
		deck: 1,
		play: 22,
		cue: 21,
		sync: 23,
		listen: 24,
		load: 25,
		jog: 48,
		vol: 54,
		treble: 55,
		medium: 56,
		bass: 57,
		cues: [13, 14, 15, 16],
		loops: [
			{ note: 9, beats: 1 },
			{ note: 10, beats: 2 },
			{ note: 11, beats: 4 },
			{ note: 12, beats: 8 }
		],
		fx: [1, 2, 3, 4],
		pbm: 17,
		pbp: 18
	},
	{
		deck: 2,
		play: 48,
		cue: 47,
		sync: 49,
		listen: 50,
		load: 51,
		jog: 49,
		vol: 59,
		treble: 60,
		medium: 61,
		bass: 62,
		cues: [39, 40, 41, 42],
		loops: [
			{ note: 35, beats: 1 },
			{ note: 36, beats: 2 },
			{ note: 37, beats: 4 }
		],
		fx: [27, 28, 29, 30],
		pbm: 43,
		pbp: 44
	}
];

function _deckBindings(spec: DeckSpec): MidiBinding[] {
	const { deck } = spec;
	return [
		{ source: note(spec.play), action: { type: 'deck_play_toggle', deck } },
		{ source: note(spec.cue), action: { type: 'deck_cue', deck } },
		{ source: note(spec.sync), action: { type: 'deck_sync_toggle', deck } },
		{ source: note(spec.listen), action: { type: 'channel_cue', deck } },
		{ source: note(spec.load), action: { type: 'browse_load', deck } },
		// Jog: 1-63 CW, 64-127 CCW. Pioneer relativeCentered is 0x41 = +1, so
		// invert makes Instinct CW a positive delta. Speed is in the value;
		// relativeUnit is unset.
		{
			source: cc(spec.jog),
			action: { type: 'deck_jog', deck },
			relative: true,
			relativeCentered: true,
			invert: true
		},
		{ source: cc(spec.vol), action: { type: 'mixer_channel', deck, target: 'fader' } },
		{
			source: cc(spec.treble),
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'high' }
		},
		{
			source: cc(spec.medium),
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'mid' }
		},
		{ source: cc(spec.bass), action: { type: 'mixer_channel', deck, target: 'eq', band: 'low' } },
		...HOT_CUE_SLOTS.map((slot, i): MidiBinding => ({
			source: note(spec.cues[i]),
			action: { type: 'deck_hot_cue', deck, slot }
		})),
		...spec.loops.map(({ note: id, beats }): MidiBinding => ({
			source: note(id),
			action: { type: 'deck_beat_loop', deck, beats }
		})),
		...COLOR_FX_EFFECTS.map((effect, i): MidiBinding => ({
			source: note(spec.fx[i]),
			action: { type: 'color_fx', deck, effect }
		})),
		{ source: note(spec.pbm), action: { type: 'deck_tempo_nudge', deck, direction: -1 } },
		{ source: note(spec.pbp), action: { type: 'deck_tempo_nudge', deck, direction: 1 } }
	];
}

const _GLOBAL_BINDINGS: MidiBinding[] = [
	// XFADER CC 58: 0 full left, 127 full right. Do not invert.
	{ source: cc(58), action: { type: 'mixer_global', target: 'crossfader' } }
];

function _deckHints(deck: Deck): ControlHint[] {
	const d = `(deck ${deck})`;
	if (deck === 1) {
		return [
			{ source: note(5), label: `SAMPLE_KP1 ${d}` },
			{ source: note(6), label: `SAMPLE_KP2 ${d}` },
			{ source: note(7), label: `SAMPLE_KP3 ${d}` },
			{ source: note(8), label: `SAMPLE_KP4 ${d}` },
			{ source: note(26), label: `TOUCH ${d}` },
			{ source: note(19), label: `PREV ${d}` },
			{ source: note(20), label: `NEXT ${d}` },
			{ source: cc(52), label: `PITCH fader ${d} (incremental)` }
		];
	}
	return [
		{ source: note(31), label: `SAMPLE_KP1 ${d}` },
		{ source: note(32), label: `SAMPLE_KP2 ${d}` },
		{ source: note(33), label: `SAMPLE_KP3 ${d}` },
		{ source: note(34), label: `SAMPLE_KP4 ${d}` },
		{ source: note(52), label: `TOUCH ${d}` },
		{ source: note(45), label: `PREV ${d}` },
		{ source: note(46), label: `NEXT ${d}` },
		{ source: cc(53), label: `PITCH fader ${d} (incremental)` }
	];
}

const _GLOBAL_HINTS: ControlHint[] = [
	{ source: note(53), label: 'SCRATCH' },
	{ source: note(54), label: 'UP' },
	{ source: note(55), label: 'DOWN' },
	{ source: note(56), label: 'FILES' },
	{ source: note(57), label: 'FOLDERS' },
	{ source: note(64), label: 'HP_VOL_M' },
	{ source: note(65), label: 'HP_VOL_P' },
	{ source: note(38), label: 'printed CUE_KP4_DB (duplicate of note 42)' }
];

function _led(trigger: LedRule['trigger'], noteId: number): LedRule {
	return {
		trigger,
		out: { ch: CH, note: noteId, velocityOn: INSTINCT_LED_ON, velocityOff: INSTINCT_LED_OFF }
	};
}

function _deckLeds(spec: DeckSpec): LedRule[] {
	const { deck } = spec;
	return [
		_led({ kind: 'deck_playing', deck }, spec.play),
		_led({ kind: 'deck_loaded', deck }, spec.cue),
		_led({ kind: 'beat_sync_enabled', deck }, spec.sync),
		_led({ kind: 'channel_cue_enabled', deck }, spec.listen),
		...HOT_CUE_SLOTS.map((slot, i) => _led({ kind: 'hot_cue_present', deck, slot }, spec.cues[i])),
		...spec.loops.map(({ note: id, beats }) =>
			_led({ kind: 'loop_beats_engaged', deck, beats }, id)
		)
	];
}

/**
 * Matches catalog/WebMIDI names such as `Hercules DJ Control Instinct`,
 * `DJ Control Instinct`, `DJ Control Instinct S`, `DJ Control Instinct MIDI 1`.
 * Rejects `DJ Control Instinct P8`. Written without lookaround so regex_lite
 * in the engine compiles it. `DJControl Instinct P8` (no space after DJ)
 * does not contain `DJ Control Instinct`, so it is already excluded.
 */
const _NAME_MATCH = 'DJ Control Instinct(?:$|\\S|\\s+(?:$|[^\\sP]|P(?:$|[^8])|P8\\w))';

export const HERCULES_DJ_CONTROL_INSTINCT_MAP: DeviceMap = {
	vendor: 'Hercules',
	nameMatch: _NAME_MATCH,
	nativeAudioProfile: 'master12-cue34',
	bindings: [...DECKS.flatMap(_deckBindings), ..._GLOBAL_BINDINGS],
	leds: DECKS.flatMap(_deckLeds),
	hints: [...DECKS.flatMap((spec) => _deckHints(spec.deck)), ..._GLOBAL_HINTS]
};

/** Self-registration: maps/entries.ts discovers this export, so no shared list names this map. */
export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{ key: 'HERCULES_DJ_CONTROL_INSTINCT_MAP', map: HERCULES_DJ_CONTROL_INSTINCT_MAP, live: true }
];
