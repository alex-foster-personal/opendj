/**
 * Hercules DJ Control AIR device map (build unit: hercules-dj-control-air map).
 *
 * SOURCE OF TRUTH: "DJ CONTROL AIR and DJUCED Reference Manual", version 1.0,
 * section D "DJ Control AIR MIDI messages" (D.1 MIDI channel p.48, D.2 MIDI
 * Input p.48-51, D.3 MIDI Output p.51).
 *   https://manualzz.docsio.net/doc/6790644/hercules-djcontrol-air--owner-s-manual
 *   https://ts.hercules.com/download/sound/manuals/DJ_AIR/DJUCED/DJUCED_Manual_v1_ENG.pdf
 *
 * Wires transcribed from the manufacturer chart only. Mixxx's GPL mapping and
 * tools/deck-diagrams/devices/hercules-dj-control-air/midi.json were NOT used
 * as a source (cross-check only). NOT HARDWARE CONFIRMED.
 *
 * ONE MIDI channel (D.1): factory default channel 1, user-changeable in the
 * Hercules control panel. Every binding here uses channel 1.
 *
 * Pitch fader direction: the D.2 chart prints no +/- end labels on the pitch
 * slider row, so deck_pitch binds without invert (direction unconfirmed).
 *
 * ACCEPTANCE DEVIATIONS (issue #10838):
 *   Effect pads + Effects mode: no beat FX / colour FX action in MidiAction;
 *   pad runtime refuses instant_fx -- chart numbers preserved as hints only.
 *   Browse: LOAD bound; Up/Down/Files/Folders are buttons with no step action.
 *   LEDs omitted: D.3 MIDI-OUT table was not retrievable as text (ddj-400 rule).
 *
 * Requirements (mini-PRD, MIDI-11983):
 *   ✔︎ Transport play/cue/sync, PFL, load per deck (D.2).
 *     [if] note 0x12 ch1 arrives [then] deck_play_toggle deck 1 ⛔️ if missing
 *   ✔︎ Loop pads 1..4 -> deck_beat_loop beats 1/2/4/8 (D.2 loop pad rows).
 *   ✔︎ Pitch bend +/- -> deck_tempo_nudge (D.2).
 *   ✔︎ Jog CC 0x30/0x32 deck 1, 0x31/0x33 deck 2, relative two's complement.
 *     [if] jog uses relativeCentered [then ⛔️] sign decode is wrong
 *   ✔︎ 14-bit pitch MSB cc 0x34/0x35, lsbOffset 36 (LSB cc 0x58/0x59 unbound).
 *   ✔︎ Volume fader + EQ treble/mid/bass (EQ invert: true per D.2 00=clockwise).
 *   ✔︎ Crossfader cc 0x3a, not inverted (D.2: 00 left, 7F right).
 */

import type {
	ControlHint,
	DeviceMap,
	DeviceMapEntry,
	MidiBinding,
	MidiSource
} from '$lib/rb/midi/midi-types';
import type { DeckId } from '$lib/rb/deck-slots';

/** Factory-default MIDI channel (D.1); user may change in Hercules panel. */
const CH = 1;

function note(id: number): MidiSource {
	return { ch: CH, kind: 'note', id };
}

function cc(id: number): MidiSource {
	return { ch: CH, kind: 'cc', id };
}

const DECKS: readonly DeckId[] = [1, 2];

/** Per-deck wire ids from D.2 (hex). */
const WIRES = {
	1: {
		cue: 0x11, // D.2 p.48 CUE A
		play: 0x12, // D.2 p.48 PLAY A
		sync: 0x13, // D.2 p.48 SYNC A
		listen: 0x14, // D.2 p.48 Listen A (PFL)
		load: 0x15, // D.2 p.48 LOAD A
		pitchBendMinus: 0x0d, // D.2 p.48 Pitch bend -
		pitchBendPlus: 0x0e, // D.2 p.48 Pitch bend +
		loopPads: [0x09, 0x0a, 0x0b, 0x0c] as const, // D.2 p.49 Loop pads A
		jog: 0x30, // D.2 p.49 Jog rotation A
		jogScratch: 0x32, // D.2 p.49 Jog rotation, scratch mode A
		pitchMsb: 0x34, // D.2 p.49 Pitch fader MSB A
		volume: 0x36, // D.2 p.49 Volume fader A
		treble: 0x37, // D.2 p.49 Treble A
		mid: 0x38, // D.2 p.49 Medium A
		bass: 0x39, // D.2 p.49 Bass A
		rewind: 0x0f, // D.2 p.48 REWIND A
		forward: 0x10, // D.2 p.48 FORWARD A
		jogPressure: 0x16, // D.2 p.48 Jog pressure A
		effectPads: [0x01, 0x02, 0x03, 0x04] as const, // D.2 p.48 Effect pads A
		samplePads: [0x05, 0x06, 0x07, 0x08] as const // D.2 p.48 Sample pads A
	},
	2: {
		cue: 0x27, // D.2 p.50 CUE B
		play: 0x28, // D.2 p.50 PLAY B
		sync: 0x29, // D.2 p.50 SYNC B
		listen: 0x2a, // D.2 p.50 Listen B
		load: 0x2b, // D.2 p.50 LOAD B
		pitchBendMinus: 0x23, // D.2 p.50 Pitch bend -
		pitchBendPlus: 0x24, // D.2 p.50 Pitch bend +
		loopPads: [0x1f, 0x20, 0x21, 0x22] as const, // D.2 p.50 Loop pads B
		jog: 0x31, // D.2 p.50 Jog rotation B
		jogScratch: 0x33, // D.2 p.50 Jog rotation, scratch mode B
		pitchMsb: 0x35, // D.2 p.50 Pitch fader MSB B
		volume: 0x3b, // D.2 p.50 Volume fader B
		treble: 0x3c, // D.2 p.50 Treble B
		mid: 0x3d, // D.2 p.50 Medium B
		bass: 0x3e, // D.2 p.50 Bass B
		rewind: 0x25, // D.2 p.50 REWIND B
		forward: 0x26, // D.2 p.50 FORWARD B
		jogPressure: 0x2c, // D.2 p.50 Jog pressure B
		effectPads: [0x17, 0x18, 0x19, 0x1a] as const, // D.2 p.50 Effect pads B
		samplePads: [0x1b, 0x1c, 0x1d, 0x1e] as const // D.2 p.50 Sample pads B
	}
} as const;

/** Loop pad beat ladder (chart documents notes only; lengths are ours). */
export const HERCULES_AIR_LOOP_PAD_BEATS: readonly number[] = [1, 2, 4, 8];

/** Pitch LSB offset: chart pairs MSB 0x34 with LSB 0x58 (+36, not MIDI +32). */
export const HERCULES_AIR_PITCH_LSB_OFFSET = 36;

function _deckBindings(deck: DeckId): MidiBinding[] {
	if (deck !== 1 && deck !== 2) {
		throw new Error(`Hercules DJ Control AIR is 2-deck only (got deck ${deck})`);
	}
	const w = WIRES[deck];
	const loopBindings = w.loopPads.map((id, i) => ({
		source: note(id),
		action: {
			type: 'deck_beat_loop' as const,
			deck,
			beats: HERCULES_AIR_LOOP_PAD_BEATS[i]
		}
	}));
	return [
		{ source: note(w.play), action: { type: 'deck_play_toggle', deck } },
		{ source: note(w.cue), action: { type: 'deck_cue', deck } },
		{ source: note(w.sync), action: { type: 'deck_sync_toggle', deck } },
		{ source: note(w.listen), action: { type: 'channel_cue', deck } },
		{ source: note(w.load), action: { type: 'browse_load', deck } },
		{
			source: note(w.pitchBendMinus),
			action: { type: 'deck_tempo_nudge', deck, direction: -1 }
		},
		{
			source: note(w.pitchBendPlus),
			action: { type: 'deck_tempo_nudge', deck, direction: 1 }
		},
		...loopBindings,
		{ source: cc(w.jog), action: { type: 'deck_jog', deck }, relative: true },
		{ source: cc(w.jogScratch), action: { type: 'deck_jog', deck }, relative: true },
		{
			source: cc(w.pitchMsb),
			action: { type: 'deck_pitch', deck, lsbOffset: HERCULES_AIR_PITCH_LSB_OFFSET }
		},
		{
			source: cc(w.volume),
			action: { type: 'mixer_channel', deck, target: 'fader' }
		},
		{
			source: cc(w.treble),
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'high' },
			invert: true
		},
		{
			source: cc(w.mid),
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

const _GLOBAL_BINDINGS: MidiBinding[] = [
	// D.2 p.51 Crossfader
	{
		source: cc(0x3a),
		action: { type: 'mixer_global', target: 'crossfader' }
	}
];

function _deckHints(deck: DeckId): ControlHint[] {
	if (deck !== 1 && deck !== 2) {
		throw new Error(`Hercules DJ Control AIR is 2-deck only (got deck ${deck})`);
	}
	const w = WIRES[deck];
	const effectHints = w.effectPads.map((id, i) => ({
		source: note(id),
		label: `EFFECT pad ${i + 1} (deck ${deck})`
	}));
	const sampleHints = w.samplePads.map((id, i) => ({
		source: note(id),
		label: `Sample pad ${i + 1} (deck ${deck})`
	}));
	return [
		{ source: note(w.rewind), label: `REWIND (deck ${deck})` },
		{ source: note(w.forward), label: `FORWARD (deck ${deck})` },
		{ source: note(w.jogPressure), label: `Jog pressure (deck ${deck})` },
		...effectHints,
		...sampleHints
	];
}

const _GLOBAL_HINTS: ControlHint[] = [
	{ source: note(0x39), label: 'Headphone Mix' },
	{ source: note(0x3a), label: 'Headphone Cue' },
	{ source: note(0x3b), label: 'Headphone volume -' },
	{ source: note(0x3c), label: 'Headphone volume +' },
	{ source: note(0x33), label: 'Browse Up' },
	{ source: note(0x34), label: 'Browse Down' },
	{ source: note(0x35), label: 'Files' },
	{ source: note(0x36), label: 'Folders' },
	{ source: note(0x37), label: 'Loops mode' },
	{ source: note(0x38), label: 'Effects mode' },
	{ source: note(0x2f), label: 'Sample mode' },
	{ source: note(0x31), label: 'Select Down' },
	{ source: note(0x32), label: 'Select Up' },
	{ source: note(0x2d), label: 'Scratch' },
	{ source: note(0x2e), label: 'Magic' },
	{ source: note(0x30), label: 'Record' },
	{ source: cc(0x3f), label: 'AIR proximity sensor' }
];

export const HERCULES_DJ_CONTROL_AIR_MAP: DeviceMap = {
	vendor: 'Hercules',
	// Excludes AIR+ and AIR S (separate charts); see DDJ-1000 (?:$|[^S]) precedent.
	nameMatch: 'DJ ?Control ?AIR(?:$|[^+\\w\\s]|\\s+[^S+\\s]|\\s+$)',
	bindings: [...DECKS.flatMap((d) => _deckBindings(d)), ..._GLOBAL_BINDINGS],
	hints: [...DECKS.flatMap((d) => _deckHints(d)), ..._GLOBAL_HINTS]
};

/** Self-registration: maps/entries.ts discovers this export, so no shared list names this map. */
export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{ key: 'HERCULES_DJ_CONTROL_AIR_MAP', map: HERCULES_DJ_CONTROL_AIR_MAP, live: true }
];
