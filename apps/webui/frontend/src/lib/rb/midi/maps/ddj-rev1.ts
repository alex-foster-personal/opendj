/**
 * Pioneer DJ DDJ-REV1 P0 device map (build unit: DDJ-REV1 map, epic #9366).
 *
 * SOURCE OF TRUTH for EVERY Note/CC number in this file: the official
 * "DDJ-REV1 List of MIDI message" PDF (E1), from
 * https://downloads.support.alphatheta.com/software_info/dj-controllers/DDJ-REV1/DDJ-REV1_MIDI_Message_List_E1.pdf
 * (not redistributed here; sha256 recorded in
 * docs/controller/research/DDJ-REV1-MIDI-Message-List.md). Rows are cited
 * as [PDF] with the sheet's own Fig. ids (D1.., M1.., F1.., P1.., B1..).
 *
 * DO NOT source numbers from tools/deck-diagrams/devices/ddj-rev1/midi.json -
 * that file is a `"bootstrap": true` stub with sequential invented codes.
 *
 * Channel layout ([PDF] p.1 "MIDI channel assignment"):
 *   deck 1 / 2 (other than pads)   -> channels 1 / 2 (decks 3 / 4 -> 3 / 4, not bound)
 *   EFFECT FX1 / FX2               -> channels 5 / 6
 *   BROWSER + global mixer         -> channel 7
 *   performance pads, unshifted    -> channels 8 (deck 1) / 10 (deck 2)
 *   performance pads, shifted      -> channels 9 / 11 (not bound)
 *
 * REV1 vs DDJ-400 / FLX4, from the sheet:
 *   - ONE SHIFT button, in the mixer section: [PDF] M11, channel 7 note 63
 *     (0x3F). The deck channels carry no SHIFT row.
 *   - No RELOOP/EXIT button: AUTO LOOP ([PDF] D6, note 0x14) starts a 4-beat
 *     loop and its +SHIFT row (note 0x50, printed RELOOP/EXIT on the panel)
 *     is the loop exit. 1/2X and 2X ([PDF] D7/D8, notes 0x12/0x13) halve and
 *     double the loop; their +SHIFT rows are LOOP IN/OUT (hinted).
 *   - SYNC ([PDF] D9, note 0x58) is bound to deck_sync_toggle.
 *   - FILTER per channel sits on the global channel 7 like the FLX4's CFX
 *     ([PDF] F1-1/F1-2: CC 23 / 24 MSB).
 *   - No headphone MIX knob: HEADPHONES LEVEL only ([PDF] M10, CC 13).
 *   - Pad AUTO LOOP mode (notes 0x10..0x17) is bound to the beat-loop ladder;
 *     the sheet's other modes (TRACKING SCRATCH, SAMPLER, BEAT JUMP, ROLL,
 *     TRANS, SCRATCH BANK) are out of P0 scope.
 *   - The 14-bit mixer caveat is the same: MSB bound, LSB CCs land in the
 *     learn log. deck_pitch alone pairs its LSB ([PDF] D4: CC 0 / 32).
 *
 * TWO DECKS, NOT FOUR: DECK SELECT ([PDF] D5-L/D5-R) switches a side to deck
 * 3 or 4 on channels 3 / 4. Those rows are hints; nothing binds deck 3 or 4.
 *
 * LED rules exist only for rows whose [PDF] MIDI-OUT column prints
 * "Same as MIDI-IN" AND whose control is bound: PLAY, CUE, AUTO LOOP (both
 * rows), SYNC, CH CUE, HOT CUE pads, AUTO LOOP pads. Pad-mode buttons, FX
 * slot buttons and DECK SELECT also print MIDI-OUT but are hints here, so no
 * rule drives them (a rule for an inert control would invent feedback).
 *
 * OUT OF CONTRACT (hints only): jog, DECK SELECT, pad-mode buttons, FX
 * paddles, FX slot buttons, LEVEL/DEPTH, fader-start notes, LOAD deck 3/4,
 * shifted twins of bound buttons, BROWSE + FX slot select.
 */

import type {
	ControlHint,
	DeviceMap,
	DeviceMapEntry,
	LedRule,
	MidiBinding
} from '$lib/rb/midi/midi-types';
import type { DeckId } from '$lib/rb/deck-slots';
import {
	pioneerCc,
	pioneerDeckChannel,
	pioneerNote,
	pioneerPadChannel,
	pioneerPadLedRules
} from './pioneer-deck-bindings';

const DECKS = [1, 2] as const;
type Deck = (typeof DECKS)[number];

/** [PDF] M11: the one SHIFT button sends on the BROWSER/mixer channel. */
const GLOBAL_CH = 7;
const FX_CH: Record<Deck, number> = { 1: 5, 2: 6 };
/** [PDF] F1-1 / F1-2 FILTER (CH1) / (CH2): CC 23 / 24 MSB on channel 7. */
const FILTER_CC: Record<Deck, number> = { 1: 0x17, 2: 0x18 };

/** Beats per AUTO LOOP pad 1..8 ([PDF] P1..P8 AUTO LOOP mode, notes 0x10..0x17).
 * The sheet documents only the wire notes; the ladder is our software
 * assignment, the same doubling ladder the DDJ-400 and FLX4 maps use. */
export const DDJ_REV1_AUTO_LOOP_PAD_BEATS: readonly number[] = [0.25, 0.5, 1, 2, 4, 8, 16, 32];
export const DDJ_REV1_PAD_ON = 0x7f;
export const DDJ_REV1_PAD_OFF = 0x00;

const HOT_CUE_SLOTS = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'] as const;

function _deckBindings(deck: Deck): MidiBinding[] {
	const ch = pioneerDeckChannel(deck);
	return [
		// [PDF] D1 PLAY/PAUSE: note 11 (0x0B).
		{ source: pioneerNote(ch, 0x0b), action: { type: 'deck_play_toggle', deck } },
		// [PDF] D2 CUE: note 12 (0x0C).
		{ source: pioneerNote(ch, 0x0c), action: { type: 'deck_cue', deck } },
		// [PDF] D4 Tempo: CC 0 MSB / CC 32 LSB, "-" side Min, "+" side Max.
		{ source: pioneerCc(ch, 0x00), action: { type: 'deck_pitch', deck, lsbOffset: 32 } },
		// [PDF] D6 AUTO LOOP: note 20 (0x14); +SHIFT note 80 (0x50) is RELOOP/EXIT.
		{ source: pioneerNote(ch, 0x14), action: { type: 'deck_beat_loop', deck, beats: 4 } },
		{ source: pioneerNote(ch, 0x50), action: { type: 'deck_loop_exit', deck } },
		// [PDF] D7 1/2X: note 18 (0x12). [PDF] D8 2X: note 19 (0x13).
		{ source: pioneerNote(ch, 0x12), action: { type: 'deck_loop_scale', deck, factor: 0.5 } },
		{ source: pioneerNote(ch, 0x13), action: { type: 'deck_loop_scale', deck, factor: 2 } },
		// [PDF] D9 SYNC: note 88 (0x58).
		{ source: pioneerNote(ch, 0x58), action: { type: 'deck_sync_toggle', deck } }
	];
}

function _mixerBindings(deck: Deck): MidiBinding[] {
	const ch = pioneerDeckChannel(deck);
	return [
		// [PDF] M3 GAIN (TRIM): CC 4 MSB.
		{ source: pioneerCc(ch, 0x04), action: { type: 'mixer_channel', deck, target: 'trim' } },
		// [PDF] M4 EQ HI: CC 7 MSB.
		{
			source: pioneerCc(ch, 0x07),
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'high' }
		},
		// [PDF] M5 EQ MID: CC 11 MSB.
		{
			source: pioneerCc(ch, 0x0b),
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'mid' }
		},
		// [PDF] M6 EQ LOW: CC 15 MSB.
		{
			source: pioneerCc(ch, 0x0f),
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'low' }
		},
		// [PDF] M2 CH FADER: CC 19 MSB, "Min at bottom end, Max at top end".
		{ source: pioneerCc(ch, 0x13), action: { type: 'mixer_channel', deck, target: 'fader' } },
		// [PDF] F1 FILTER (CHn): CC 23 / 24 MSB on channel 7.
		{
			source: pioneerCc(GLOBAL_CH, FILTER_CC[deck]),
			action: { type: 'mixer_channel', deck, target: 'filter' }
		},
		// [PDF] M7 CUE (Headphone): note 84 (0x54).
		{ source: pioneerNote(ch, 0x54), action: { type: 'channel_cue', deck } }
	];
}

function _padBindings(deck: Deck): MidiBinding[] {
	// [PDF] P1..P8: HOT CUE mode notes 0..7, AUTO LOOP mode notes 0x10..0x17,
	// on the deck's unshifted pad channel.
	const ch = pioneerPadChannel(deck);
	const out: MidiBinding[] = [];
	for (let pad = 0; pad < 8; pad++) {
		out.push({
			source: pioneerNote(ch, pad),
			action: { type: 'deck_hot_cue', deck, slot: HOT_CUE_SLOTS[pad] }
		});
		out.push({
			source: pioneerNote(ch, 0x10 + pad),
			action: { type: 'deck_beat_loop', deck, beats: DDJ_REV1_AUTO_LOOP_PAD_BEATS[pad] }
		});
	}
	return out;
}

function _globalBindings(): MidiBinding[] {
	const ch = GLOBAL_CH;
	return [
		// [PDF] B1 Rotary Selector rotate: CC 64 (0x40); +SHIFT CC 100 (0x64). Both relative.
		{ source: pioneerCc(ch, 0x40), action: { type: 'browse_encoder' }, relative: true },
		{ source: pioneerCc(ch, 0x64), action: { type: 'browse_encoder' }, relative: true },
		// [PDF] B2-L / B2-R LOAD, deck 1 / deck 2 selected: notes 70 (0x46) / 71 (0x47).
		{ source: pioneerNote(ch, 0x46), action: { type: 'browse_load', deck: 1 } },
		{ source: pioneerNote(ch, 0x47), action: { type: 'browse_load', deck: 2 } },
		// [PDF] M1 CROSSFADER: CC 31 (0x1F) MSB, Min left, Max right.
		{ source: pioneerCc(ch, 0x1f), action: { type: 'mixer_global', target: 'crossfader' } },
		// [PDF] M9 MASTER LEVEL: CC 8 MSB.
		{ source: pioneerCc(ch, 0x08), action: { type: 'mixer_global', target: 'master' } },
		// [PDF] M10 HEADPHONES LEVEL: CC 13 (0x0D) MSB. The REV1 has no MIX knob.
		{ source: pioneerCc(ch, 0x0d), action: { type: 'headphone_level' } },
		// [PDF] M8 MASTER CUE: note 99 (0x63).
		{ source: pioneerNote(ch, 0x63), action: { type: 'master_cue', mode: 'latch' } },
		// [PDF] M11 SHIFT: note 63 (0x3F) on channel 7.
		{ source: pioneerNote(ch, 0x3f), action: { type: 'shift_modifier' } }
	];
}

function _deckHints(deck: Deck): ControlHint[] {
	const ch = pioneerDeckChannel(deck);
	const alt = deck + 2;
	return [
		// [PDF] D1 / D2 +SHIFT.
		{ source: pioneerNote(ch, 0x47), label: `PLAY/PAUSE + SHIFT (deck ${deck})` },
		{ source: pioneerNote(ch, 0x48), label: `CUE + SHIFT (deck ${deck})` },
		// [PDF] D3 JOG (Platter) and (Wheel Side).
		{ source: pioneerCc(ch, 0x22), label: `JOG platter, vinyl on (deck ${deck})` },
		{ source: pioneerCc(ch, 0x23), label: `JOG platter, vinyl off (deck ${deck})` },
		{ source: pioneerCc(ch, 0x1f), label: `JOG platter + SHIFT (deck ${deck})` },
		{ source: pioneerNote(ch, 0x36), label: `JOG touch (deck ${deck})` },
		{ source: pioneerNote(ch, 0x67), label: `JOG touch + SHIFT (deck ${deck})` },
		{ source: pioneerCc(ch, 0x21), label: `JOG wheel side (deck ${deck})` },
		{ source: pioneerCc(ch, 0x26), label: `JOG wheel side + SHIFT (deck ${deck})` },
		// [PDF] D4 Tempo +SHIFT: CC 5 MSB / 37 LSB.
		{ source: pioneerCc(ch, 0x05), label: `TEMPO + SHIFT (deck ${deck})` },
		// [PDF] D5 DECK SELECT: press note 114 (0x72) on the alternate deck's channel;
		// state notes 60 (0x3C) on both; +SHIFT note 23 (0x17); +LONG note 96 (0x60).
		{ source: pioneerNote(alt, 0x72), label: `DECK SELECT ${deck}/${alt}` },
		{ source: pioneerNote(ch, 0x3c), label: `DECK SELECT: deck ${deck} control on/off` },
		{ source: pioneerNote(alt, 0x3c), label: `DECK SELECT: deck ${alt} control on/off` },
		{ source: pioneerNote(ch, 0x17), label: `DECK SELECT + SHIFT (deck ${deck})` },
		{ source: pioneerNote(alt, 0x17), label: `DECK SELECT + SHIFT (deck ${alt})` },
		{ source: pioneerNote(ch, 0x60), label: `DECK SELECT long press (deck ${deck})` },
		{ source: pioneerNote(alt, 0x60), label: `DECK SELECT long press (deck ${alt})` },
		// [PDF] D7 / D8 +SHIFT: LOOP IN / LOOP OUT. [PDF] D9 +SHIFT: SYNC OFF.
		{ source: pioneerNote(ch, 0x10), label: `LOOP IN (1/2X + SHIFT, deck ${deck})` },
		{ source: pioneerNote(ch, 0x11), label: `LOOP OUT (2X + SHIFT, deck ${deck})` },
		{ source: pioneerNote(ch, 0x5c), label: `SYNC OFF (SYNC + SHIFT, deck ${deck})` },
		// [PDF] M1/M2 fader start (+SHIFT while sliding).
		{ source: pioneerNote(ch, 0x66), label: `fader start PLAY (deck ${deck})` },
		{ source: pioneerNote(ch, 0x51), label: `fader start SYNC (deck ${deck})` },
		{ source: pioneerNote(ch, 0x52), label: `fader start CUE (deck ${deck})` },
		// [PDF] M7 CUE (Headphone) +SHIFT.
		{ source: pioneerNote(ch, 0x68), label: `CH CUE + SHIFT (deck ${deck})` },
		// [PDF] P9..P12 pad-mode buttons and their +SHIFT rows.
		{ source: pioneerNote(ch, 0x1b), label: `HOT CUE MODE (deck ${deck})` },
		{ source: pioneerNote(ch, 0x69), label: `BEAT JUMP MODE (HOT CUE + SHIFT, deck ${deck})` },
		{ source: pioneerNote(ch, 0x1e), label: `AUTO LOOP MODE (deck ${deck})` },
		{ source: pioneerNote(ch, 0x6b), label: `ROLL MODE (AUTO LOOP + SHIFT, deck ${deck})` },
		{ source: pioneerNote(ch, 0x20), label: `TRACKING SCRATCH MODE (deck ${deck})` },
		{ source: pioneerNote(ch, 0x6d), label: `TRANS MODE (TRACKING + SHIFT, deck ${deck})` },
		{ source: pioneerNote(ch, 0x22), label: `SAMPLER MODE (deck ${deck})` },
		{ source: pioneerNote(ch, 0x6f), label: `SCRATCH BANK MODE (SAMPLER + SHIFT, deck ${deck})` }
	];
}

function _fxHints(unit: Deck): ControlHint[] {
	const ch = FX_CH[unit];
	return [
		// [PDF] F2 FX ON/OFF paddle: note 80 (0x50); +SHIFT note 102 (0x66); per-slot ON notes 71..73.
		{ source: pioneerNote(ch, 0x50), label: `FX ${unit} ON/OFF paddle` },
		{ source: pioneerNote(ch, 0x66), label: `FX ${unit} ON/OFF paddle + SHIFT` },
		{ source: pioneerNote(ch, 0x47), label: `FX ${unit} slot 1 ON` },
		{ source: pioneerNote(ch, 0x48), label: `FX ${unit} slot 2 ON` },
		{ source: pioneerNote(ch, 0x49), label: `FX ${unit} slot 3 ON` },
		// [PDF] F3 / F4 / F5 slot select buttons: notes 112..114 (0x70..0x72); +SHIFT 99 / 6 / 7.
		{ source: pioneerNote(ch, 0x70), label: `FX ${unit} slot 1 button` },
		{ source: pioneerNote(ch, 0x63), label: `FX ${unit} select (slot 1 + SHIFT)` },
		{ source: pioneerNote(ch, 0x71), label: `FX ${unit} slot 2 button` },
		{ source: pioneerNote(ch, 0x06), label: `FX ${unit} beat back (slot 2 + SHIFT)` },
		{ source: pioneerNote(ch, 0x72), label: `FX ${unit} slot 3 button` },
		{ source: pioneerNote(ch, 0x07), label: `FX ${unit} beat forward (slot 3 + SHIFT)` },
		// [PDF] F6 LEVEL/DEPTH: CC 2 / 4 / 6 MSB per lit slot.
		{ source: pioneerCc(ch, 0x02), label: `FX ${unit} LEVEL/DEPTH (slot 1)` },
		{ source: pioneerCc(ch, 0x04), label: `FX ${unit} LEVEL/DEPTH (slot 2)` },
		{ source: pioneerCc(ch, 0x06), label: `FX ${unit} LEVEL/DEPTH (slot 3)` },
		// [PDF] B1 Rotary Selector + FX slot select: CC 80..82 (FX1) / 83..85 (FX2).
		...[0, 1, 2].map((slot): ControlHint => ({
			source: pioneerCc(ch, 0x50 + slot + (unit - 1) * 3),
			label: `BROWSE + FX ${unit} slot ${slot + 1} select`
		}))
	];
}

function _globalHints(): ControlHint[] {
	const ch = GLOBAL_CH;
	return [
		// [PDF] B1 Rotary Selector press: note 65 (0x41); +SHIFT note 66 (0x42).
		{ source: pioneerNote(ch, 0x41), label: 'BROWSE press' },
		{ source: pioneerNote(ch, 0x42), label: 'BROWSE press + SHIFT' },
		// [PDF] B2 LOAD +SHIFT (deck 1 / 2): notes 88 (0x58) / 89 (0x59); deck 3 / 4 rows.
		{ source: pioneerNote(ch, 0x58), label: 'LOAD deck 1 + SHIFT' },
		{ source: pioneerNote(ch, 0x59), label: 'LOAD deck 2 + SHIFT' },
		{ source: pioneerNote(ch, 0x48), label: 'LOAD deck 3' },
		{ source: pioneerNote(ch, 0x60), label: 'LOAD deck 3 + SHIFT' },
		{ source: pioneerNote(ch, 0x49), label: 'LOAD deck 4' },
		{ source: pioneerNote(ch, 0x61), label: 'LOAD deck 4 + SHIFT' },
		// [PDF] M8 MASTER CUE +SHIFT: note 98 (0x62).
		{ source: pioneerNote(ch, 0x62), label: 'MASTER CUE + SHIFT' },
		// [PDF] F1 FILTER (CH3) / (CH4): CC 25 / 26 MSB.
		{ source: pioneerCc(ch, 0x19), label: 'FILTER (deck 3)' },
		{ source: pioneerCc(ch, 0x1a), label: 'FILTER (deck 4)' }
	];
}

function _ledRules(deck: Deck): LedRule[] {
	const ch = pioneerDeckChannel(deck);
	const padCh = pioneerPadChannel(deck);
	const on = DDJ_REV1_PAD_ON;
	const off = DDJ_REV1_PAD_OFF;
	const rules: LedRule[] = [
		// [PDF] D1 PLAY/PAUSE MIDI-OUT "Same as MIDI-IN".
		{
			trigger: { kind: 'deck_playing', deck },
			out: { ch, note: 0x0b, velocityOn: on, velocityOff: off }
		},
		// [PDF] D2 CUE MIDI-OUT. Software choice (ours): lit when a track is loaded.
		{
			trigger: { kind: 'deck_loaded', deck },
			out: { ch, note: 0x0c, velocityOn: on, velocityOff: off }
		},
		// [PDF] D6 AUTO LOOP MIDI-OUT, both the plain and the +SHIFT row.
		{
			trigger: { kind: 'loop_engaged', deck },
			out: { ch, note: 0x14, velocityOn: on, velocityOff: off }
		},
		{
			trigger: { kind: 'loop_engaged', deck },
			out: { ch, note: 0x50, velocityOn: on, velocityOff: off }
		},
		// [PDF] D9 SYNC MIDI-OUT.
		{
			trigger: { kind: 'beat_sync_enabled', deck },
			out: { ch, note: 0x58, velocityOn: on, velocityOff: off }
		},
		// [PDF] M7 CUE (Headphone) MIDI-OUT.
		{
			trigger: { kind: 'channel_cue_enabled', deck },
			out: { ch, note: 0x54, velocityOn: on, velocityOff: off }
		},
		// [PDF] P1..P8 HOT CUE mode MIDI-OUT.
		...pioneerPadLedRules(deck, on, off)
	];
	// [PDF] P1..P8 AUTO LOOP mode MIDI-OUT: lit while that pad's loop length is engaged.
	DDJ_REV1_AUTO_LOOP_PAD_BEATS.forEach((beats, pad) => {
		rules.push({
			trigger: { kind: 'loop_beats_engaged', deck, beats },
			out: { ch: padCh, note: 0x10 + pad, velocityOn: on, velocityOff: off }
		});
	});
	return rules;
}

export const DDJ_REV1_MAP: DeviceMap = {
	vendor: 'Pioneer DJ',
	// WebMIDI port names carry the product name, e.g. "DDJ-REV1", "Pioneer DJ DDJ-REV1 MIDI 1".
	nameMatch: 'DDJ-REV1',
	bindings: [
		...DECKS.flatMap((deck) => _deckBindings(deck)),
		...DECKS.flatMap((deck) => _mixerBindings(deck)),
		...DECKS.flatMap((deck) => _padBindings(deck)),
		..._globalBindings()
	],
	leds: DECKS.flatMap((deck) => _ledRules(deck)),
	hints: [
		...DECKS.flatMap((deck) => _deckHints(deck)),
		...DECKS.flatMap((unit) => _fxHints(unit)),
		..._globalHints()
	]
};

/** Self-registration: maps/entries.ts discovers this export, so no shared list names this map. */
export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{ key: 'DDJ_REV1_MAP', map: DDJ_REV1_MAP, live: true }
];
