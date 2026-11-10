/**
 * Numark Mixstream Pro practice-replica device map (issue #10870, ADR-0137).
 *
 * HINTS ONLY, NO BINDINGS. Numark publishes no MIDI message list for this controller (the
 * User Guide v5.0.0 and product page carry none; docs/controller/research/
 * Numark-Mixstream-Pro-sources.md records the search). Every wire below is transcribed from the
 * community Mixxx mapping Numark-Mixstream-Pro.midi.xml and Numark-Mixstream-Pro.scripts.js
 * (GPL-2.0-or-later, authors danielkinahan and ptrex; sha256 in the research note), tier 2 of
 * (private path). SHIFT is software state in the Mixxx script
 * (deckState.shift); no separate shifted note numbers.
 *
 * NOT in DEVICE_MAP_REGISTRY: the engine export requires at least one binding per registered map.
 * The map is registered for the practice route only (PRACTICE_DEVICE_MAPS).
 *
 * Channel layout ([Mixxx] status bytes):
 *   channel strip EQ / fader / PFL     -> channels 1 / 2
 *   deck transport, pads, jog, pitch -> channels 3 / 4
 *   FX select and toggles              -> channels 5 / 6
 *   browse, load, master, headphones   -> channel 16
 *
 * MIDI-OUT: VU meters CC 0x20/0x21 on status 0xBF when enableVUMeter is true in the script.
 */

import type {
	ControlHint,
	DeviceMap,
	DeviceMapEntry,
	MidiMeterOutput,
	MidiSource
} from '$lib/rb/midi/midi-types';

const note = (ch: number, id: number): MidiSource => ({ ch, kind: 'note', id });
const cc = (ch: number, id: number): MidiSource => ({ ch, kind: 'cc', id });

function _deckHints(deck: 3 | 4): ControlHint[] {
	const ch = deck;
	const d = `(deck ${deck === 3 ? 1 : 2})`;
	return [
		{ source: cc(ch, 55), label: `JOG turn MSB ${d}` },
		{ source: cc(ch, 31), label: `PITCH fader MSB ${d}` },
		{ source: note(ch, 29), label: `PITCH BEND - ${d}` },
		{ source: note(ch, 30), label: `PITCH BEND + ${d}` },
		{ source: note(ch, 35), label: `SCRATCH mode ${d}` },
		{ source: note(ch, 8), label: `SYNC ${d}` },
		{ source: note(ch, 9), label: `CUE ${d}` },
		{ source: note(ch, 10), label: `PLAY/PAUSE ${d}` },
		{ source: note(ch, 28), label: `SHIFT ${d}` },
		{ source: note(ch, 11), label: `pad mode CUE / stems ${d}` },
		{ source: note(ch, 12), label: `pad mode SAVED LOOP ${d}` },
		{ source: note(ch, 14), label: `pad mode AUTO LOOP ${d}` },
		{ source: note(ch, 13), label: `pad mode ROLL / sampler ${d}` },
		{ source: note(ch, 15), label: `PAD 1 ${d}` },
		{ source: note(ch, 16), label: `PAD 2 ${d}` },
		{ source: note(ch, 17), label: `PAD 3 ${d}` },
		{ source: note(ch, 18), label: `PAD 4 ${d}` }
	];
}

function _stripHints(ch: 1 | 2): ControlHint[] {
	const d = `(channel ${ch})`;
	return [
		{ source: cc(ch, 3), label: `LEVEL ${d}` },
		{ source: cc(ch, 8), label: `EQ HIGH ${d}` },
		{ source: cc(ch, 6), label: `EQ MID ${d}` },
		{ source: cc(ch, 4), label: `EQ LOW ${d}` },
		{ source: cc(ch, 11), label: `FILTER ${d}` },
		{ source: note(ch, 13), label: `CUE/PFL ${d}` },
		{ source: cc(ch, 14), label: `CH FADER ${d}` }
	];
}

function _fxHints(): ControlHint[] {
	return [
		{ source: note(5, 0), label: 'FX ECHO' },
		{ source: note(5, 1), label: 'FX DELAY' },
		{ source: note(5, 2), label: 'FX FLANGER' },
		{ source: note(5, 3), label: 'FX PHASER' },
		{ source: note(5, 4), label: 'FX toggle (deck 1 column)' },
		{ source: note(6, 4), label: 'FX toggle (deck 2 column)' }
	];
}

function _globalHints(): ControlHint[] {
	return [
		{ source: cc(16, 5), label: 'BROWSE turn' },
		{ source: note(16, 6), label: 'BROWSE press (GoToItem)' },
		{ source: note(16, 1), label: 'LOAD 1' },
		{ source: note(16, 2), label: 'LOAD 2' },
		{ source: note(16, 3), label: 'BACK' },
		{ source: note(16, 16), label: 'VIEW' },
		{ source: cc(16, 9), label: 'HEADPHONE MIX' },
		{ source: cc(16, 10), label: 'HEADPHONE VOLUME' },
		{ source: cc(16, 14), label: 'CROSSFADER' },
		{ source: cc(16, 15), label: 'MAIN LEVEL' },
		{ source: cc(16, 18), label: 'SPEAKER LEVEL' }
	];
}

const METERS: MidiMeterOutput[] = [
	{ deck: 1, out: { ch: 16, cc: 32, maxValue: 127 } },
	{ deck: 2, out: { ch: 16, cc: 33, maxValue: 127 } }
];

export const NUMARK_MIXSTREAM_PRO_MAP: DeviceMap = {
	vendor: 'Numark',
	nameMatch: 'Mixstream\\s*Pro(?!\\s*(?:\\+|Plus|Go))',
	bindings: [],
	meters: METERS,
	hints: [
		..._deckHints(3),
		..._deckHints(4),
		..._stripHints(1),
		..._stripHints(2),
		..._fxHints(),
		..._globalHints()
	]
};

export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{ key: 'NUMARK_MIXSTREAM_PRO_MAP', map: NUMARK_MIXSTREAM_PRO_MAP, live: false }
];
