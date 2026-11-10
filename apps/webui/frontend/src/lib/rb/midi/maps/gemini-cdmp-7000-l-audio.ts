/**
 * Gemini Gemini CDMP-7000 L audio device map (issue #10828).
 *
 * SOURCE OF TRUTH for every Send Number: Gemini CDMP-7000 user manual (PDF
 * created 19 Nov 2012, 18,429,390 bytes), English page 19, table "MIDI DEVICE
 * MESSAGES TABLE". Provenance: docs/controller/research/Gemini-CDMP-7000-MIDI-Device-Messages-Table.md
 * (sha256 cec06edcbc28f996c118b5a458dc67949b8d253d9b0cb224a9824d4ebe94b210).
 * The original geminidj.com URL is no longer resolvable; public mirrors include
 * manualmachine.com/gemini/cdmp7000/21367903-user-manual and
 * manuals.online/gemini/dj-equipment/cdmp-7000/user-manual/21rb3 (not byte-checked).
 *
 * The chart has no channel or note/CC column. Every binding uses ch 1 (the L
 * port is Deck 1). Note/CC kind was cross-checked read-only against the Mixxx
 * GPL preset Gemini CDMP-7000 L audio.midi.xml; nothing was copied from it.
 *
 * Bound (Send # -> action, all ch 1, deck 1):
 *   1 Cue -> deck_cue; 2 Play/Pause -> deck_play_toggle; 3/4 pitch bend -> tempo
 *   nudge +/-; 5/6/7 HotCue 1-3 -> slots A/B/C; 13 MASTER TEMPO -> deck_sync_toggle
 *   (repurpose: no SYNC button on hardware); 15 LOAD; 16/17 loop in/out -> manual
 *   loop cycle; 18 reloop/exit -> loop exit; 32 jogwheel CC -> deck_jog relative
 *   centred on 64; 33 pitch slider CC 7-bit -> deck_pitch lsbOffset null.
 *
 * Documented but not wired (kind unverifiable on chart alone): 23, 25, 26, 27,
 * 37, 38, 39, 40, 42, 43, 44 (includes 14-bit pitch row 44).
 *
 * Jog: chart min "0, 63" and max "63, 128" -> relativeCentered on 64 (clockwise
 * direction unconfirmed). Pitch slider: plain 7-bit; orientation unconfirmed, no
 * invert. Pitch bend +/-: momentary on hardware -> deck_tempo_nudge per press.
 *
 * Mixer: analog; chart has no channel fader, crossfader, EQ or trim rows.
 * FX: effect select/param/dry-wet/hold are hints only; MidiAction has no beat FX
 * or colour FX case. Do NOT bind Dry/Wet to mixer filter.
 * Browse: 11/12 and 34/35 are note buttons; browse_encoder needs relative CC, so
 * hints only; LOAD (15) is bound.
 *
 * Scope: CDMP-7000 L port (Deck 1) only; right deck is issue #10829.
 * No LED rules: second table is HID input, not MIDI output.
 *
 * NOT HARDWARE CONFIRMED. No wire here has been checked on a unit.
 */

import type { ControlHint, DeviceMap, DeviceMapEntry, MidiBinding } from '$lib/rb/midi/midi-types';

const CH = 1;
const DECK = 1 as const;

function note(id: number): { ch: number; kind: 'note'; id: number } {
	return { ch: CH, kind: 'note', id };
}

function cc(id: number): { ch: number; kind: 'cc'; id: number } {
	return { ch: CH, kind: 'cc', id };
}

function hintNote(id: number, label: string): ControlHint {
	return { source: note(id), label: `${label} (deck ${DECK})` };
}

function hintCc(id: number, label: string): ControlHint {
	return { source: cc(id), label: `${label} (deck ${DECK})` };
}

const BINDINGS: MidiBinding[] = [
	{ source: note(1), action: { type: 'deck_cue', deck: DECK } },
	{ source: note(2), action: { type: 'deck_play_toggle', deck: DECK } },
	{ source: note(3), action: { type: 'deck_tempo_nudge', deck: DECK, direction: 1 } },
	{ source: note(4), action: { type: 'deck_tempo_nudge', deck: DECK, direction: -1 } },
	{ source: note(5), action: { type: 'deck_hot_cue', deck: DECK, slot: 'A' } },
	{ source: note(6), action: { type: 'deck_hot_cue', deck: DECK, slot: 'B' } },
	{ source: note(7), action: { type: 'deck_hot_cue', deck: DECK, slot: 'C' } },
	{ source: note(13), action: { type: 'deck_sync_toggle', deck: DECK } },
	{ source: note(15), action: { type: 'browse_load', deck: DECK } },
	{ source: note(16), action: { type: 'deck_manual_loop_cycle', deck: DECK } },
	{ source: note(17), action: { type: 'deck_manual_loop_cycle', deck: DECK } },
	{ source: note(18), action: { type: 'deck_loop_exit', deck: DECK } },
	{
		source: cc(32),
		action: { type: 'deck_jog', deck: DECK },
		relative: true,
		relativeCentered: true
	},
	{ source: cc(33), action: { type: 'deck_pitch', deck: DECK, lsbOffset: null } }
];

const HINTS: ControlHint[] = [
	hintNote(8, 'MEMO'),
	hintNote(9, 'TRACK SEARCH <<'),
	hintNote(10, 'TRACK SEARCH >>'),
	hintNote(11, 'BROWSE LEFT (up in tracklist)'),
	hintNote(12, 'BROWSE RIGHT (down in tracklist)'),
	hintNote(14, 'VINYL'),
	hintNote(19, 'BPM'),
	hintNote(20, 'EFFECT 1 (wah) select'),
	hintNote(21, 'EFFECT 2 (flanger) select'),
	hintNote(22, 'EFFECT 3 (echo) select'),
	hintNote(24, 'JOG TOUCH'),
	hintNote(28, 'LOCK'),
	hintNote(29, 'RANGE'),
	hintNote(30, 'REVERSE'),
	hintNote(31, 'SLIP'),
	hintNote(34, 'TRACK SKIP <<'),
	hintNote(35, 'TRACK SKIP >>'),
	hintNote(36, 'HOLD (effect on/off)'),
	hintCc(41, 'DRY/WET (effect)')
];

export const GEMINI_CDMP7000_L_MAP: DeviceMap = {
	vendor: 'Gemini',
	nameMatch: 'CDMP-7000 L',
	bindings: BINDINGS,
	leds: [],
	hints: HINTS
};

/** Self-registration: maps/entries.ts discovers this export, so no shared list names this map. */
export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{ key: 'GEMINI_CDMP7000_L_MAP', map: GEMINI_CDMP7000_L_MAP, live: true }
];
