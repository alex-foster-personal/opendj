/**
 * Denon DJ DN-SC2000 device map (issue #10822).
 *
 * SOURCE OF TRUTH: Denon DJ DN-SC2000 Owner's Manual, section "MIDI command list /
 * Data transmission" ("Send CMD" and "LED data reception" tables). Mirrors that agree:
 * https://www.manual.ec/denon-dj/dn-sc2000/manual and
 * https://www.manual.sv/denon/dn-sc2000/manual. Denon serves no standalone MIDI PDF for
 * this model. Manual MIDI channel n=0..7 maps to repo `ch` = n+1.
 *
 * Clean room: Mixxx (GPL) was read only to cross-check ch1/ch2 wire parity, never as a
 * wire source. tools/deck-diagrams/devices/denon-dn-sc2000/midi.json is not a source.
 *
 * HARDWARE: not connected to this code (unconfirmed). Provenance lives in this header,
 * as in denon-prime4-plus.ts and ddj-rev1.ts.
 *
 * Channel layout: two deck layers (DECK CHG). Default pair CH1-2 only: deck 1 = ch 1,
 * deck 2 = ch 2, identical wires. A unit on CH3-4 / CH5-6 / CH7-8 (SHIFT+PREVIOUS/NEXT/
 * DUPLICATE) will not resolve against these bindings.
 *
 * This unit has NO channel fader, crossfader, EQ, trim, master, headphone or LOAD
 * control. TRACK SELECT knob press (note 0x28) loads onto the current layer's deck.
 *
 * Unverified on hardware: knob rows print "Inc 0x00 / Dec 0x7F"; under the repo's
 * two's-complement decode, 0x00 becomes 0 (a dropped tick). Pitch slider orientation
 * is not stated in the chart, so `invert` is unset on deck_pitch.
 *
 * OUT OF CONTRACT (hints only): FX block (no beat-FX or colour-FX MidiAction), FILTER
 * knob (relative encoder vs absolute mixer takeover), LOOP IN / LOOP OUT, BEAT ±, BEND ±,
 * FAST SEARCH ±, KEY LOCK, jog touch note, DECK CHG, SEL., NEXT, PREVIOUS, BROWSER,
 * DUPLICATE, CUE/LOOP DELETE. LED reception is CC-addressed (ON 0x4A / OFF 0x4B);
 * LedRule emits Note On only, so this map declares no leds.
 *
 * Requirements (mini-PRD):
 *   [if] play on ch 1 note 0x43 arrives [then] deck 1 play toggles, [else stop ⛔️]
 *   [if] any binding (ch, kind, id) is absent from the owner's-manual chart [then ⛔️]
 */

import type { ControlHint, DeviceMap, DeviceMapEntry, MidiBinding } from '$lib/rb/midi/midi-types';
import type { DeckId } from '$lib/rb/deck-slots';
import type { HotCueSlot } from '$lib/rb/hot-cue-types';

const HOT_CUE_NOTES = [0x17, 0x18, 0x19, 0x20, 0x21, 0x22, 0x23, 0x24] as const;
const HOT_CUE_SLOTS: readonly HotCueSlot[] = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];

const HINT_NOTE_SPECS: readonly { id: number; label: string }[] = [
	{ id: 0x03, label: 'DECK CHG.' },
	{ id: 0x06, label: 'KEY LOCK' },
	{ id: 0x0c, label: 'BEND +' },
	{ id: 0x0d, label: 'BEND -' },
	{ id: 0x10, label: 'FAST SEARCH +' },
	{ id: 0x11, label: 'FAST SEARCH -' },
	{ id: 0x12, label: 'PARAM.1 SW (FX1)' },
	{ id: 0x13, label: 'PARAM.2 SW (FX1)' },
	{ id: 0x14, label: 'PARAM.3 SW (FX1)' },
	{ id: 0x15, label: 'DRY/WET SW (FX1)' },
	{ id: 0x1a, label: 'SEL.' },
	{ id: 0x29, label: 'NEXT' },
	{ id: 0x30, label: 'PREVIOUS' },
	{ id: 0x37, label: 'LOOP IN' },
	{ id: 0x39, label: 'LOOP OUT' },
	{ id: 0x51, label: 'JOGWHEEL TOUCH' },
	{ id: 0x52, label: 'PARAM.1 SW (FX2)' },
	{ id: 0x53, label: 'PARAM.2 SW (FX2)' },
	{ id: 0x54, label: 'PARAM.3 SW (FX2)' },
	{ id: 0x55, label: 'DRY/WET SW (FX2)' },
	{ id: 0x56, label: 'FX ON 1' },
	{ id: 0x57, label: 'FX ON 2' },
	{ id: 0x58, label: 'FX UNIT SW' },
	{ id: 0x60, label: 'CUE/LOOP DELETE (SHIFT)' },
	{ id: 0x64, label: 'BROWSER' },
	{ id: 0x65, label: 'DUPLICATE' },
	{ id: 0x68, label: 'FILTER SW' },
	{ id: 0x69, label: 'BEAT -' },
	{ id: 0x6a, label: 'BEAT +' }
];

const HINT_CC_SPECS: readonly { id: number; label: string }[] = [
	{ id: 0x55, label: 'DRY/WET KNOB (FX1)' },
	{ id: 0x56, label: 'PARAM.1 KNOB (FX1)' },
	{ id: 0x57, label: 'PARAM.2 KNOB (FX1)' },
	{ id: 0x58, label: 'PARAM.3 KNOB (FX1)' },
	{ id: 0x59, label: 'DRY/WET KNOB (FX2)' },
	{ id: 0x5a, label: 'PARAM.1 KNOB (FX2)' },
	{ id: 0x5b, label: 'PARAM.2 KNOB (FX2)' },
	{ id: 0x5c, label: 'PARAM.3 KNOB (FX2)' },
	{ id: 0x5d, label: 'FILTER KNOB' }
];

function _deckBindings(deck: DeckId): MidiBinding[] {
	const ch = deck;
	return [
		{ source: { ch, kind: 'note', id: 0x43 }, action: { type: 'deck_play_toggle', deck } },
		{ source: { ch, kind: 'note', id: 0x42 }, action: { type: 'deck_cue', deck } },
		{ source: { ch, kind: 'note', id: 0x6b }, action: { type: 'deck_sync_toggle', deck } },
		{
			source: { ch, kind: 'cc', id: 0x51 },
			action: { type: 'deck_jog', deck },
			relative: true,
			relativeCentered: true
		},
		{
			source: { ch, kind: 'pitchbend', id: 0 },
			action: { type: 'deck_pitch', deck, lsbOffset: null }
		},
		...HOT_CUE_NOTES.map((note, index): MidiBinding => ({
			source: { ch, kind: 'note', id: note },
			action: { type: 'deck_hot_cue', deck, slot: HOT_CUE_SLOTS[index] }
		})),
		{ source: { ch, kind: 'note', id: 0x1d }, action: { type: 'deck_auto_loop_toggle', deck } },
		{ source: { ch, kind: 'cc', id: 0x54 }, action: { type: 'browse_encoder' }, relative: true },
		{ source: { ch, kind: 'note', id: 0x28 }, action: { type: 'browse_load', deck } }
	];
}

function _deckHints(deck: DeckId): ControlHint[] {
	const ch = deck;
	const deckTag = `(deck ${deck})`;
	return [
		...HINT_NOTE_SPECS.map((row): ControlHint => ({
			source: { ch, kind: 'note', id: row.id },
			label: `${row.label} ${deckTag}`
		})),
		...HINT_CC_SPECS.map((row): ControlHint => ({
			source: { ch, kind: 'cc', id: row.id },
			label: `${row.label} ${deckTag}`
		}))
	];
}

/**
 * nameMatch: the chart prints no USB descriptor. Its compliance block says "USB MIDI" and
 * Windows shows "USB Audio Device"; both would hijack generic ports. Catalog model is
 * "Denon DN SC2000" (tools/deck-diagrams/catalog/controllers.json, id denon-dn-sc2000).
 */
export const DENON_DN_SC2000_MAP: DeviceMap = {
	vendor: 'Denon DJ',
	nameMatch: 'DN[- ]?SC[- ]?2000',
	bindings: [..._deckBindings(1), ..._deckBindings(2)],
	hints: [..._deckHints(1), ..._deckHints(2)]
};

export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{ key: 'DENON_DN_SC2000_MAP', map: DENON_DN_SC2000_MAP, live: true }
];
