/**
 * DJ-Tech Kontrol One practice-replica device map (issue #10860, ADR-0137).
 *
 * HINTS ONLY, NO BINDINGS (Path B). The vendor User Manual PDF (manualslib id 1387269, MIDI MAP
 * pages 14-15) could not be obtained on the builder host: manualslib is Cloudflare-blocked and
 * dj-tech.com legacy download URLs 404. Without a tier-1 MIDI table this map does not bind PLAY,
 * CUE, SYNC or SHIFT; every wire below is a hint so controls stay inert with the PARITY-TODO
 * title until a PDF capture or hardware spot-check promotes rows to bindings.
 *
 * SOURCES (every wire cites one of these; none invented):
 *   [Mixxx] DJ-Tech Kontrol One.midi.xml and DJ-Tech-Kontrol-One-scripts.js (GPL-2.0-or-later),
 *         https://github.com/mixxxdj/mixxx/blob/main/res/controllers/DJ-Tech%20Kontrol%20One.midi.xml
 *         Local ingest sha256 8f2c4249a6ac9b32317d1e74e0b25a5a32a724c1c75532c779dd891e7c99a1bd
 *         (tools/deck-diagrams/devices/dj-tech-kontrol-one/midi.json); tier 2 of
 *         (private path).
 *
 * NOT in DEVICE_MAP_REGISTRY: the engine export requires at least one binding per registered map;
 * a real Kontrol One should stay in the learn log as unmapped until tier-1 PDF or capture exists.
 * Registered for the practice route only (PRACTICE_DEVICE_MAPS).
 *
 * Channel layout ([Mixxx] status bytes on 9X / BX, X = 0..3): channel N is deck N. The hardware
 * exposes one bank; DECK SELECT picks the active channel in firmware (no input wire in the Mixxx map).
 * SHIFT sends the same note whether held or not; there are no shift-twin wires.
 *
 * Requirements (mini-PRD):
 *   [if] a Kontrol One panel wire is absent from hints [then ⛔️] incomplete
 *   [if] a hint here uses a wire absent from [Mixxx] ingest [then ⛔️] reject
 */

import type { ControlHint, DeviceMap, DeviceMapEntry } from '$lib/rb/midi/midi-types';
import type { DeckId } from '$lib/rb/deck-slots';

const DECKS: readonly DeckId[] = [1, 2, 3, 4];

const note = (ch: number, id: number) => ({ ch, kind: 'note' as const, id });
const cc = (ch: number, id: number) => ({ ch, kind: 'cc' as const, id });

function _deckHints(deck: DeckId): ControlHint[] {
	const ch = deck;
	const d = `(deck ${deck})`;
	return [
		// [Mixxx] transport row (vendor PDF not verified; hint only)
		{ source: note(ch, 0x1b), label: `PLAY ${d}` },
		{ source: note(ch, 0x1c), label: `CUE ${d}` },
		{ source: note(ch, 0x1d), label: `SYNC ${d}` },
		{ source: note(ch, 0x1a), label: `SHIFT ${d}` },
		// [Mixxx] RLP1..RLP4 encoder rings / k1..k4
		{ source: cc(ch, 0x37), label: `KNOB 1 ${d}` },
		{ source: cc(ch, 0x36), label: `KNOB 2 ${d}` },
		{ source: cc(ch, 0x35), label: `KNOB 3 ${d}` },
		{ source: cc(ch, 0x34), label: `KNOB 4 ${d}` },
		// knob push kb1..kb4
		{ source: note(ch, 0x04), label: `KNOB 1 PUSH ${d}` },
		{ source: note(ch, 0x03), label: `KNOB 2 PUSH ${d}` },
		{ source: note(ch, 0x02), label: `KNOB 3 PUSH ${d}` },
		{ source: note(ch, 0x01), label: `KNOB 4 PUSH ${d}` },
		// [Mixxx] grid row 1..4
		{ source: note(ch, 0x09), label: `REC1 ${d}` },
		{ source: note(ch, 0x08), label: `REC2 ${d}` },
		{ source: note(ch, 0x07), label: `REC3 ${d}` },
		{ source: note(ch, 0x06), label: `REC4 ${d}` },
		{ source: note(ch, 0x0e), label: `SAMPLE1 ${d}` },
		{ source: note(ch, 0x0d), label: `SAMPLE2 ${d}` },
		{ source: note(ch, 0x0c), label: `SAMPLE3 ${d}` },
		{ source: note(ch, 0x0b), label: `SAMPLE4 ${d}` },
		{ source: note(ch, 0x13), label: `CUE1 ${d}` },
		{ source: note(ch, 0x12), label: `CUE2 ${d}` },
		{ source: note(ch, 0x11), label: `CUE3 ${d}` },
		{ source: note(ch, 0x10), label: `CUE4 ${d}` },
		{ source: note(ch, 0x18), label: `LOOP1 ${d}` },
		{ source: note(ch, 0x17), label: `LOOP2 ${d}` },
		{ source: note(ch, 0x16), label: `LOOP3 ${d}` },
		{ source: note(ch, 0x15), label: `LOOP4 ${d}` },
		{ source: note(ch, 0x19), label: `PITCH - ${d}` },
		{ source: note(ch, 0x1e), label: `PITCH + ${d}` },
		{ source: note(ch, 0x1f), label: `JOG TOUCH ${d}` },
		{ source: cc(ch, 0x20), label: `JOG WHEEL ${d}` },
		{ source: cc(ch, 0x21), label: `PITCH SLIDER ${d}` }
	];
}

const hints: ControlHint[] = DECKS.flatMap(_deckHints);

export const DJ_TECH_KONTROL_ONE_MAP: DeviceMap = {
	vendor: 'DJ-Tech',
	nameMatch: 'Kontrol ?(One|1)',
	bindings: [],
	hints
};

export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{ key: 'DJ_TECH_KONTROL_ONE_MAP', map: DJ_TECH_KONTROL_ONE_MAP, live: false }
];
