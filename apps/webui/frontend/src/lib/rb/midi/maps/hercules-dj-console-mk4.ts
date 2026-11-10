/**
 * Hercules DJ Console Mk4 live device map (issue #10834).
 *
 * SOURCE OF TRUTH: Hercules "DJ Console MK4 Operation Manual", section 10
 * "CONFIGURATION IN MIDI MODE", pages 26-28 of 32 (input rows p.26-27, output
 * p.28), cited as [MAN]. Manufacturer FTP copy no longer served. Readable
 * mirrors (not redistributed):
 *   https://www.manualslib.com/manual/3084708/Hercules-Mk4.html?page=26
 *   https://manualzilla.com/doc/5801224/-english--2.2-mb?page=26
 *
 * Encoding: buttons are Note On with 127 pressed / 0 released (no ccButton).
 * JOG and PITCH CCs send incremental 1..63 CW, 127..64 CCW (two's complement,
 * NOT centred). VOL, EQ, XFADER and CUE2Mix are absolute 0..127; chart labels
 * EQ and CUE2Mix as "Full CW : Full CCW" so 0 is full clockwise.
 *
 * The [MAN] chart lists MIDI Command (decimal) only, not a channel. Every wire
 * here uses MIDI channel 1 as an assumption (Mk2 manual same vendor family
 * documents channel 1; cross-check only, not a second source of wires).
 *
 * EQ bands and CUE2Mix use invert: true because 0 = full CW on the chart and
 * headphone_mix value 1 is master (Mix end CW). Orientation is NOT HARDWARE
 * CONFIRMED.
 *
 * Out of contract: pitch CCs are relative incremental, so hinted only (deck_pitch
 * needs absolute). The chart has no beat-FX or colour-FX control and MidiAction
 * has no FX case, so none is bound. Source, Shift, Shift_State, Prev/Next
 * (no track-skip action), Menu_U/D, Files, Folders and Scratch are hints.
 * Browse encoder needs a relative CC; Menu_U/D are notes only. p.28 LED outputs
 * do not map to LedRule inputs, so leds: [].
 *
 * NOT HARDWARE CONFIRMED.
 *
 * Requirements (mini-PRD, MIDI-10834):
 *   ✔︎ Play, cue, sync, PFL, load, hot cues A-H, tempo nudge, jog, faders, EQ,
 *     crossfader and headphone cue/mix bind [MAN] section 10 wires on channel 1.
 *     [if] a binding's (kind, id) is absent from section 10 [then ⛔️] invent wire
 *     [if] nameMatch misses "Hercules DJ Console MK4" or hits Mk2/4-Mx [then ⛔️]
 */

import type {
	ControlHint,
	DeviceMap,
	DeviceMapEntry,
	MidiBinding,
	MidiSource
} from '$lib/rb/midi/midi-types';
import type { DeckId } from '$lib/rb/deck-slots';

/** [MAN] gives (kind, id) only; channel 1 is an assumption (see header). */
const CH = 1;

const note = (id: number): MidiSource => ({ ch: CH, kind: 'note', id });
const cc = (id: number): MidiSource => ({ ch: CH, kind: 'cc', id });

const HOT_CUE_SLOTS = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'] as const;

interface DeckWires {
	deck: DeckId;
	play: number;
	cue: number;
	sync: number;
	listen: number;
	load: number;
	pads: readonly [number, number, number, number];
	shiftedPads: readonly [number, number, number, number];
	bendMinus: number;
	bendPlus: number;
	jog: number;
	volume: number;
	treble: number;
	medium: number;
	bass: number;
	prev: number;
	next: number;
	shift: number;
	shiftState: number;
	source: number;
	pitch: number;
}

const DECKS: readonly DeckWires[] = [
	{
		deck: 1,
		play: 15, // [MAN] p.27 Play_DA
		cue: 14, // [MAN] p.27 Cue_DA
		sync: 18, // [MAN] p.27 Sync_DA
		listen: 16, // [MAN] p.27 Listen_DA
		load: 17, // [MAN] p.27 Load_DA
		pads: [1, 2, 3, 4], // [MAN] p.26 K1_DA..K4_DA
		shiftedPads: [5, 6, 7, 8], // [MAN] p.26 SHIFTED_K1_DA..SHIFTED_K4_DA
		bendMinus: 10, // [MAN] p.27 PBM_DA
		bendPlus: 11, // [MAN] p.27 PBP_DA
		jog: 48, // [MAN] p.27 JOG_DA
		volume: 52, // [MAN] p.27 VOL_DA
		treble: 53, // [MAN] p.27 TREBLE_DA
		medium: 54, // [MAN] p.27 MEDIUM_DA
		bass: 55, // [MAN] p.27 BASS_DA
		prev: 12, // [MAN] p.27 Prev_DA
		next: 13, // [MAN] p.27 Next_DA
		shift: 9, // [MAN] p.26 Shift_DA
		shiftState: 20, // [MAN] p.27 Shift_State_DA
		source: 19, // [MAN] p.26 Source_DA
		pitch: 50 // [MAN] p.27 PITCH_DA
	},
	{
		deck: 2,
		play: 35, // [MAN] p.27 Play_DB
		cue: 34, // [MAN] p.27 Cue_DB
		sync: 38, // [MAN] p.27 Sync_DB
		listen: 36, // [MAN] p.27 Listen_DB
		load: 37, // [MAN] p.27 Load_DB
		pads: [21, 22, 23, 24], // [MAN] p.27 K1_DB..K4_DB
		shiftedPads: [25, 26, 27, 28], // [MAN] p.27 SHIFTED_K1_DB..SHIFTED_K4_DB
		bendMinus: 30, // [MAN] p.27 PBM_DB
		bendPlus: 31, // [MAN] p.27 PBP_DB
		jog: 49, // [MAN] p.27 JOG_DB
		volume: 57, // [MAN] p.27 VOL_DB
		treble: 58, // [MAN] p.27 TREBLE_DB
		medium: 59, // [MAN] p.27 MEDIUM_DB
		bass: 60, // [MAN] p.27 BASS_DB
		prev: 32, // [MAN] p.27 Prev_DB
		next: 33, // [MAN] p.27 Next_DB
		shift: 29, // [MAN] p.27 Shift_DB
		shiftState: 40, // [MAN] p.27 Shift_State_DB
		source: 39, // [MAN] p.27 Source_DB
		pitch: 51 // [MAN] p.27 PITCH_DB
	}
];

function _deckBindings(w: DeckWires): MidiBinding[] {
	const { deck } = w;
	const padBindings = w.pads.map((id, i) => ({
		source: note(id),
		action: { type: 'deck_hot_cue' as const, deck, slot: HOT_CUE_SLOTS[i] }
	}));
	const shiftedPadBindings = w.shiftedPads.map((id, i) => ({
		source: note(id),
		action: { type: 'deck_hot_cue' as const, deck, slot: HOT_CUE_SLOTS[i + 4] }
	}));
	return [
		{ source: note(w.play), action: { type: 'deck_play_toggle', deck } },
		{ source: note(w.cue), action: { type: 'deck_cue', deck } },
		{ source: note(w.sync), action: { type: 'deck_sync_toggle', deck } },
		{ source: note(w.listen), action: { type: 'channel_cue', deck } },
		{ source: note(w.load), action: { type: 'browse_load', deck } },
		...padBindings,
		...shiftedPadBindings,
		{
			source: note(w.bendMinus),
			action: { type: 'deck_tempo_nudge', deck, direction: -1 }
		},
		{
			source: note(w.bendPlus),
			action: { type: 'deck_tempo_nudge', deck, direction: 1 }
		},
		{ source: cc(w.jog), action: { type: 'deck_jog', deck }, relative: true },
		{ source: cc(w.volume), action: { type: 'mixer_channel', deck, target: 'fader' } },
		{
			source: cc(w.treble),
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'high' },
			invert: true
		},
		{
			source: cc(w.medium),
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'mid' },
			invert: true
		},
		{
			source: cc(w.bass),
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'low' },
			invert: true
		}
	];
}

function _deckHints(w: DeckWires): ControlHint[] {
	const { deck } = w;
	return [
		{ source: note(w.prev), label: `Prev Track (deck ${deck})` },
		{ source: note(w.next), label: `Next Track (deck ${deck})` },
		{ source: note(w.shift), label: `Shift (deck ${deck})` },
		{ source: note(w.shiftState), label: `Shift State (deck ${deck})` },
		{ source: note(w.source), label: `Source Select (deck ${deck})` },
		{ source: cc(w.pitch), label: `Pitch (deck ${deck})` }
	];
}

const _GLOBAL_BINDINGS: MidiBinding[] = [
	{ source: cc(56), action: { type: 'mixer_global', target: 'crossfader' } }, // [MAN] p.27 XFADER
	{ source: cc(61), action: { type: 'headphone_mix' }, invert: true } // [MAN] p.27 CUE2Mix
];

const _GLOBAL_HINTS: ControlHint[] = [
	{ source: note(41), label: 'Menu Up' }, // [MAN] p.27 Menu_U
	{ source: note(42), label: 'Menu Down' }, // [MAN] p.27 Menu_D
	{ source: note(43), label: 'Files' }, // [MAN] p.27 Files
	{ source: note(44), label: 'Folders' }, // [MAN] p.27 Folders
	{ source: note(45), label: 'Scratch' } // [MAN] p.27 Scratch
];

export const HERCULES_DJ_CONSOLE_MK4_MAP: DeviceMap = {
	vendor: 'Hercules',
	nameMatch: 'DJ Console\\s*Mk\\s*4\\b',
	bindings: [...DECKS.flatMap(_deckBindings), ..._GLOBAL_BINDINGS],
	leds: [],
	hints: [...DECKS.flatMap(_deckHints), ..._GLOBAL_HINTS]
};

/** Self-registration: maps/entries.ts discovers this export, so no shared list names this map. */
export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{ key: 'HERCULES_DJ_CONSOLE_MK4_MAP', map: HERCULES_DJ_CONSOLE_MK4_MAP, live: true }
];
