/**
 * Pioneer DJ DDJ-SX3 device map (epic #9366, CORE deck replica).
 *
 * SOURCE OF TRUTH for EVERY Note/CC number in this file: the official
 * Pioneer DJ "DDJ-SX3 List of MIDI messages" version 1.00 (E1, 2018),
 * https://downloads.support.alphatheta.com/software_info/dj-controllers/DDJ-SX3/DDJ-SX3_MIDI_Message_List_E1.pdf
 * (not redistributed here; sha256 recorded in
 * tools/deck-diagrams/devices/ddj-sx3/panel.reference.json). Cited below as
 * [PDF] with the list's own Fig. numbers (1-1 PLAY/PAUSE, 3-3 TRIM, 5-5 PAD 1).
 *
 * Channel layout ([PDF] MIDI channel assignment):
 *   DECK 1..4 (non-pad)          -> channels 1..4
 *   FX DECK1/DECK3, FX DECK2/4   -> channels 5, 6
 *   Browser, global section      -> channel 7
 *   PERFORMANCE PAD deck 1..4    -> channels 8, 9, 10, 11 (NOT the FLX4's 8/10)
 *
 * SHIFT NOTE: like every Pioneer DDJ, the SX3 encodes SHIFT in hardware: a
 * shifted control sends a DIFFERENT data1 on the same channel (PLAY sends
 * note 71 instead of 11, [PDF] 1-1). This map therefore binds shift_modifier
 * only so midiState.shiftHeld stays truthful; shifted rows are their own
 * sources, bound where the P0 union has an action and hinted otherwise.
 *
 * 14-BIT CAVEAT: every SX3 knob and fader is an MSB/LSB CC pair. deck_pitch
 * pairs CC 0/32; every other control binds its MSB CC and the LSB CCs land in
 * the learn log as unmapped, the same choice as the FLX4 and FLX10 maps.
 *
 * FOUR DECKS: the two physical decks switch between 1/3 and 2/4 with the DECK
 * buttons ([PDF] 1-11..1-14) and then send on channel 3 or 4, so the deck and
 * pad rows are bound for all four channels like the FLX10 map.
 *
 * OUT OF CONTRACT (hints only, no invented actions): NEEDLE SEARCH strip,
 * CENSOR, QUANTIZE, SLIP, KEY LOCK, DECK select, SLOT/REC/START (Serato Flip),
 * LOOP IN/OUT, pad-mode buttons, PARAMETER arrows, every FX unit control, the
 * four SOUND COLOR FX buttons, SAMPLER VOLUME, BACK, LOAD PREPARE, USB A/B,
 * shifted twins. MASTER LEVEL, BOOTH, HEADPHONES LEVEL/MIX and MIC3 LEVEL
 * send no MIDI at all ([PDF] 3-12, 3-19, 3-22, 3-23, 3-24).
 *
 * Requirements (mini-PRD):
 *   ✔︎ Transport, sync, pitch, jog, hot-cue pads, mixer strips, crossfader,
 *     browse and LOAD for four decks bind the P0 actions ([PDF] 1-1, 1-2,
 *     1-3, 1-6, 1-17, 3-3, 3-4, 3-6, 3-7, 3-9, 3-11, 3-18, 4-1, 4-2, 5-5..5-12).
 *     [if] note 0x0B ch 3 arrives [then] deck_play_toggle deck 3
 *     [if] note 2 ch 9 arrives [then] deck_hot_cue deck 2 slot C
 *   ✔︎ AUTO LOOP, LOOP 1/2x and LOOP 2x bind the loop actions ([PDF] 1-21..1-23).
 *   ✔︎ KEY UP / KEY DOWN nudge the key; KEY UP + SHIFT is KEY SYNC ([PDF] 1-9, 1-10).
 *   ✔︎ LED rules for every MIDI-OUT row the list documents on a bound control.
 *   ✔︎ Zero fabricated numbers: each block comments its [PDF] Fig. row.
 */

import type {
	ControlHint,
	DeviceMap,
	DeviceMapEntry,
	LedRule,
	MidiBinding
} from '$lib/rb/midi/midi-types';
import type { DeckId } from '$lib/rb/deck-slots';
import type { HotCueSlot } from '$lib/rb/hot-cue-types';
import { pioneerCc, pioneerDeckChannel, pioneerNote } from './pioneer-deck-bindings';

// -------------------------------------------------------------- constants

const DECKS: readonly DeckId[] = [1, 2, 3, 4];

/** [PDF] channel table: PERFORMANCE PAD deck n -> channel 7 + n. */
export function sx3PadChannel(deck: DeckId): number {
	return 7 + deck;
}

/** [PDF] 5-5..5-12 HOT CUE mode: pad k sends note k-1. Slot lettering is ours. */
const HOT_CUE_SLOTS: readonly HotCueSlot[] = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];

/** [PDF] pad MIDI-OUT: "lit in color specified in color number (1-64), OFF=0x00
 * (dimmer)". Which colour each number shows is not published, so one fixed
 * value inside the documented range is used. */
export const SX3_PAD_COLOR_ON = 0x40;
export const SX3_PAD_COLOR_OFF = 0x00;

/** [PDF] channel table: Browser, Global Section -> channel 7. */
const GLOBAL_CH = 7;
/** [PDF] channel table: FX DECK1/DECK3 -> channel 5, FX DECK2/DECK4 -> channel 6. */
const FX_CH: Record<1 | 2, number> = { 1: 5, 2: 6 };
/** [PDF] 3-6 COLOR: SOUND COLOR FX ch1..ch4 -> CC 23..26 MSB on channel 7. */
const COLOR_CC: Record<DeckId, number> = { 1: 0x17, 2: 0x18, 3: 0x19, 4: 0x1a };

// ------------------------------------------------------------ deck strips

function _deckBindings(deck: DeckId): MidiBinding[] {
	const ch = pioneerDeckChannel(deck);
	const jog = (cc: number): MidiBinding => ({
		source: pioneerCc(ch, cc),
		action: { type: 'deck_jog', deck },
		relative: true,
		relativeCentered: true
	});
	return [
		// [PDF] 1-1 PLAY/PAUSE: note 11 (0x0B).
		{ source: pioneerNote(ch, 0x0b), action: { type: 'deck_play_toggle', deck } },
		// [PDF] 1-2 CUE: note 12 (0x0C).
		{ source: pioneerNote(ch, 0x0c), action: { type: 'deck_cue', deck } },
		// [PDF] 1-3 SYNC: note 93 (0x5D).
		{ source: pioneerNote(ch, 0x5d), action: { type: 'deck_sync_toggle', deck } },
		// [PDF] 1-4 SHIFT: note 63 (0x3F).
		{ source: pioneerNote(ch, 0x3f), action: { type: 'shift_modifier' } },
		// [PDF] 1-6 JOG platter: CC 34 (0x22) vinyl on, CC 35 (0x23) vinyl off; wheel side CC 33 (0x21).
		// Relative, centred on 0x40 ("increases from 0x41 / decreases from 0x3F").
		jog(0x22),
		jog(0x23),
		jog(0x21),
		// [PDF] 1-9 KEY UP/SYNC: press note 121 (0x79), +SHIFT note 100 (0x64) = KEY SYNC.
		{ source: pioneerNote(ch, 0x79), action: { type: 'deck_key_nudge', deck, semitones: 1 } },
		{ source: pioneerNote(ch, 0x64), action: { type: 'deck_key_sync_toggle', deck } },
		// [PDF] 1-10 KEY DOWN/RESET: press note 10 (0x0A). (+SHIFT 0x65 = RESET, hinted.)
		{ source: pioneerNote(ch, 0x0a), action: { type: 'deck_key_nudge', deck, semitones: -1 } },
		// [PDF] 1-17 TEMPO: CC 0 MSB / CC 32 LSB.
		{ source: pioneerCc(ch, 0x00), action: { type: 'deck_pitch', deck, lsbOffset: 32 } },
		// [PDF] 1-21 AUTO LOOP: note 20 (0x14).
		{ source: pioneerNote(ch, 0x14), action: { type: 'deck_auto_loop_toggle', deck } },
		// [PDF] 1-22 LOOP 1/2x: note 18 (0x12). [PDF] 1-23 LOOP 2x: note 19 (0x13).
		{ source: pioneerNote(ch, 0x12), action: { type: 'deck_loop_scale', deck, factor: 0.5 } },
		{ source: pioneerNote(ch, 0x13), action: { type: 'deck_loop_scale', deck, factor: 2 } }
	];
}

function _padBindings(deck: DeckId): MidiBinding[] {
	const ch = sx3PadChannel(deck);
	// [PDF] 5-5..5-12 PERFORMANCE PAD 1..8 in HOT CUE mode: notes 0..7.
	return HOT_CUE_SLOTS.map((slot, pad) => ({
		source: pioneerNote(ch, pad),
		action: { type: 'deck_hot_cue', deck, slot }
	}));
}

// ----------------------------------------------------------------- mixer

function _mixerBindings(deck: DeckId): MidiBinding[] {
	const ch = pioneerDeckChannel(deck);
	return [
		// [PDF] 3-3 TRIM: CC 4 MSB (LSB 36 unbound).
		{ source: pioneerCc(ch, 0x04), action: { type: 'mixer_channel', deck, target: 'trim' } },
		// [PDF] 3-4 EQ HIGH / MID / LOW: CC 7 / 11 / 15 MSB (LSB 39 / 43 / 47 unbound).
		{
			source: pioneerCc(ch, 0x07),
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'high' }
		},
		{
			source: pioneerCc(ch, 0x0b),
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'mid' }
		},
		{
			source: pioneerCc(ch, 0x0f),
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'low' }
		},
		// [PDF] 3-6 COLOR (SOUND COLOR FX ch n): CC 23+n-1 MSB on channel 7, not the deck channel.
		{
			source: pioneerCc(GLOBAL_CH, COLOR_CC[deck]),
			action: { type: 'mixer_channel', deck, target: 'filter' }
		},
		// [PDF] 3-7 HEADPHONES CUE: note 84 (0x54).
		{ source: pioneerNote(ch, 0x54), action: { type: 'channel_cue', deck } },
		// [PDF] 3-9 CH FADER: CC 19 MSB (LSB 51 unbound), 0 at bottom, no invert.
		{ source: pioneerCc(ch, 0x13), action: { type: 'mixer_channel', deck, target: 'fader' } }
	];
}

function _globalBindings(): MidiBinding[] {
	const ch = GLOBAL_CH;
	return [
		// [PDF] 4-1 BROWSE rotate: CC 64 (0x40), +SHIFT CC 100 (0x64); relative ticks.
		{ source: pioneerCc(ch, 0x40), action: { type: 'browse_encoder' }, relative: true },
		{ source: pioneerCc(ch, 0x64), action: { type: 'browse_encoder' }, relative: true },
		// [PDF] 4-2 LOAD deck 1..4: notes 70..73 (0x46..0x49).
		{ source: pioneerNote(ch, 0x46), action: { type: 'browse_load', deck: 1 } },
		{ source: pioneerNote(ch, 0x47), action: { type: 'browse_load', deck: 2 } },
		{ source: pioneerNote(ch, 0x48), action: { type: 'browse_load', deck: 3 } },
		{ source: pioneerNote(ch, 0x49), action: { type: 'browse_load', deck: 4 } },
		// [PDF] 3-11 CROSSFADER: CC 31 (0x1F) MSB (LSB 63 unbound), 0 at left.
		{ source: pioneerCc(ch, 0x1f), action: { type: 'mixer_global', target: 'crossfader' } },
		// [PDF] 3-18 MASTER CUE: note 99 (0x63).
		{ source: pioneerNote(ch, 0x63), action: { type: 'master_cue', mode: 'latch' } }
	];
}

// ----------------------------------------------------------------- hints

/** Documented-but-unbound deck controls ([PDF] group 1 and 5 rows). */
function _deckHints(deck: DeckId): ControlHint[] {
	const ch = pioneerDeckChannel(deck);
	const d = `(deck ${deck})`;
	return [
		{ source: pioneerNote(ch, 0x47), label: `PLAY/PAUSE + SHIFT ${d}` },
		{ source: pioneerNote(ch, 0x48), label: `CUE + SHIFT ${d}` },
		{ source: pioneerNote(ch, 0x5c), label: `SYNC + SHIFT (SYNC OFF) ${d}` },
		// [PDF] 1-5 QUANTIZE: note 53 (0x35), +SHIFT 57 (0x39).
		{ source: pioneerNote(ch, 0x35), label: `QUANTIZE ${d}` },
		{ source: pioneerNote(ch, 0x39), label: `QUANTIZE + SHIFT ${d}` },
		// [PDF] 1-6 JOG: platter + SHIFT CC 31 (0x1F), touch note 54 (0x36), touch + SHIFT 103 (0x67), wheel side + SHIFT CC 38 (0x26).
		{ source: pioneerCc(ch, 0x1f), label: `JOG platter + SHIFT ${d}` },
		{ source: pioneerNote(ch, 0x36), label: `JOG touch ${d}` },
		{ source: pioneerNote(ch, 0x67), label: `JOG touch + SHIFT ${d}` },
		{ source: pioneerCc(ch, 0x26), label: `JOG wheel side + SHIFT ${d}` },
		// [PDF] 1-7 NEEDLE SEARCH: CC 3 (0x03) position, note 67 (0x43) touch; +SHIFT CC 40 (0x28), note 68 (0x44).
		{ source: pioneerCc(ch, 0x03), label: `NEEDLE SEARCH ${d}` },
		{ source: pioneerNote(ch, 0x43), label: `NEEDLE SEARCH touch ${d}` },
		{ source: pioneerCc(ch, 0x28), label: `NEEDLE SEARCH + SHIFT ${d}` },
		{ source: pioneerNote(ch, 0x44), label: `NEEDLE SEARCH touch + SHIFT ${d}` },
		// [PDF] 1-8 CENSOR (REV): note 21 (0x15), +SHIFT 56 (0x38).
		{ source: pioneerNote(ch, 0x15), label: `CENSOR ${d}` },
		{ source: pioneerNote(ch, 0x38), label: `CENSOR + SHIFT (REVERSE) ${d}` },
		// [PDF] 1-10 KEY DOWN/RESET + SHIFT: note 101 (0x65).
		{ source: pioneerNote(ch, 0x65), label: `KEY RESET ${d}` },
		// [PDF] 1-11..1-14 DECK 1/2/3/4: note 114 (0x72), +SHIFT 115 (0x73).
		{ source: pioneerNote(ch, 0x72), label: `DECK ${deck} select` },
		{ source: pioneerNote(ch, 0x73), label: `DECK ${deck} select + SHIFT` },
		// [PDF] 1-15 SLIP: note 64 (0x40), +SHIFT 23 (0x17) = VINYL.
		{ source: pioneerNote(ch, 0x40), label: `SLIP ${d}` },
		{ source: pioneerNote(ch, 0x17), label: `SLIP + SHIFT (VINYL) ${d}` },
		// [PDF] 1-16 KEYLOCK: note 26 (0x1A), +SHIFT 96 (0x60) = TEMPO RANGE, long press 28 (0x1C).
		{ source: pioneerNote(ch, 0x1a), label: `KEY LOCK ${d}` },
		{ source: pioneerNote(ch, 0x60), label: `KEY LOCK + SHIFT (TEMPO RANGE) ${d}` },
		{ source: pioneerNote(ch, 0x1c), label: `KEY LOCK long press ${d}` },
		// [PDF] 1-17 TEMPO + SHIFT: CC 5 (0x05) MSB.
		{ source: pioneerCc(ch, 0x05), label: `TEMPO + SHIFT ${d}` },
		// [PDF] 1-18 SLOT: note 73 (0x49), +SHIFT 89 (0x59) = SAVE.
		{ source: pioneerNote(ch, 0x49), label: `SLOT ${d}` },
		{ source: pioneerNote(ch, 0x59), label: `SLOT + SHIFT (SAVE) ${d}` },
		// [PDF] 1-19 REC: note 74 (0x4A), +SHIFT 90 (0x5A) = LOOP.
		{ source: pioneerNote(ch, 0x4a), label: `REC ${d}` },
		{ source: pioneerNote(ch, 0x5a), label: `REC + SHIFT (LOOP) ${d}` },
		// [PDF] 1-20 START: note 75 (0x4B), +SHIFT 91 (0x5B) = ON/OFF.
		{ source: pioneerNote(ch, 0x4b), label: `START ${d}` },
		{ source: pioneerNote(ch, 0x5b), label: `START + SHIFT (ON/OFF) ${d}` },
		// [PDF] 1-21 AUTO LOOP + SHIFT: note 80 (0x50) = LOOP ACTIVE.
		{ source: pioneerNote(ch, 0x50), label: `AUTO LOOP + SHIFT (LOOP ACTIVE) ${d}` },
		// [PDF] 1-22 / 1-23 LOOP 1/2x, 2x + SHIFT: notes 97 (0x61), 98 (0x62) = LOOP SHIFT.
		{ source: pioneerNote(ch, 0x61), label: `LOOP 1/2x + SHIFT (LOOP SHIFT back) ${d}` },
		{ source: pioneerNote(ch, 0x62), label: `LOOP 2x + SHIFT (LOOP SHIFT forward) ${d}` },
		// [PDF] 1-24 LOOP IN: note 16 (0x10), +SHIFT 76 (0x4C) = SLOT SELECT, long press 69 (0x45).
		{ source: pioneerNote(ch, 0x10), label: `LOOP IN ${d}` },
		{ source: pioneerNote(ch, 0x4c), label: `LOOP IN + SHIFT (SLOT SELECT) ${d}` },
		{ source: pioneerNote(ch, 0x45), label: `LOOP IN long press ${d}` },
		// [PDF] 1-25 LOOP OUT: note 17 (0x11), +SHIFT 77 (0x4D) = RELOOP/EXIT.
		{ source: pioneerNote(ch, 0x11), label: `LOOP OUT ${d}` },
		{ source: pioneerNote(ch, 0x4d), label: `LOOP OUT + SHIFT (RELOOP/EXIT) ${d}` },
		// [PDF] 3-7 / 3-8 HEADPHONES CUE + SHIFT: note 104 (0x68) = MIC REVERB.
		{ source: pioneerNote(ch, 0x68), label: `CH CUE + SHIFT (MIC REVERB) (channel ${deck})` },
		// [PDF] 5-1..5-4 pad mode buttons: HOT CUE 27 (0x1B) / ROLL 30 (0x1E) / SLICER 32 (0x20) / SAMPLER 34 (0x22),
		// +SHIFT 105 (0x69) / 107 (0x6B) / 109 (0x6D) / 110 (0x6E).
		{ source: pioneerNote(ch, 0x1b), label: `HOT CUE mode ${d}` },
		{ source: pioneerNote(ch, 0x69), label: `HOT CUE mode + SHIFT (CUE LOOP) ${d}` },
		{ source: pioneerNote(ch, 0x1e), label: `ROLL mode ${d}` },
		{ source: pioneerNote(ch, 0x6b), label: `ROLL mode + SHIFT (SAVED LOOP) ${d}` },
		{ source: pioneerNote(ch, 0x20), label: `SLICER mode ${d}` },
		{ source: pioneerNote(ch, 0x6d), label: `SLICER mode + SHIFT (SLICER LOOP) ${d}` },
		{ source: pioneerNote(ch, 0x22), label: `SAMPLER mode ${d}` },
		{ source: pioneerNote(ch, 0x6e), label: `SAMPLER mode + SHIFT (PITCH PLAY) ${d}` },
		// [PDF] 5-13 / 5-14 PARAMETER 1 left / right in HOT CUE mode: notes 36 (0x24) / 44 (0x2C), +SHIFT 1 (0x01) / 9 (0x09).
		{ source: pioneerNote(ch, 0x24), label: `PARAMETER 1 left ${d}` },
		{ source: pioneerNote(ch, 0x01), label: `PARAMETER 1 left + SHIFT (PARAMETER 2) ${d}` },
		{ source: pioneerNote(ch, 0x2c), label: `PARAMETER 1 right ${d}` },
		{ source: pioneerNote(ch, 0x09), label: `PARAMETER 1 right + SHIFT (PARAMETER 2) ${d}` }
	];
}

/** Pad rows other than HOT CUE ([PDF] 5-5..5-12 per-mode rows). */
function _padHints(deck: DeckId): ControlHint[] {
	const ch = sx3PadChannel(deck);
	const out: ControlHint[] = [];
	for (let pad = 0; pad < 8; pad++) {
		out.push({
			source: pioneerNote(ch, 0x08 + pad),
			label: `PAD ${pad + 1} in SAVED FLIP mode (deck ${deck})`
		});
		out.push({
			source: pioneerNote(ch, 0x10 + pad),
			label: `PAD ${pad + 1} in ROLL mode (deck ${deck})`
		});
		out.push({
			source: pioneerNote(ch, 0x20 + pad),
			label: `PAD ${pad + 1} in SLICER mode (deck ${deck})`
		});
		out.push({
			source: pioneerNote(ch, 0x30 + pad),
			label: `PAD ${pad + 1} in SAMPLER mode (deck ${deck})`
		});
	}
	return out;
}

/** [PDF] group 2 EFFECT: both FX units are hinted; the P0 union has no beat-FX action. */
function _fxHints(unit: 1 | 2): ControlHint[] {
	const ch = FX_CH[unit];
	const u = `FX${unit}`;
	const assignBase = unit === 1 ? 0x50 : 0x54; // [PDF] 2-8/2-9 FX Assign on channel 7.
	return [
		// [PDF] 2-1..2-3 FX knobs 1..3: CC 2/4/6 MSB, +SHIFT CC 18/20/22 MSB.
		{ source: pioneerCc(ch, 0x02), label: `${u} knob 1` },
		{ source: pioneerCc(ch, 0x12), label: `${u} knob 1 + SHIFT` },
		{ source: pioneerCc(ch, 0x04), label: `${u} knob 2` },
		{ source: pioneerCc(ch, 0x14), label: `${u} knob 2 + SHIFT` },
		{ source: pioneerCc(ch, 0x06), label: `${u} knob 3` },
		{ source: pioneerCc(ch, 0x16), label: `${u} knob 3 + SHIFT` },
		// [PDF] 2-4 FX BEAT: rotate CC 0, +SHIFT CC 16; press note 67 (0x43), +SHIFT 64 (0x40).
		{ source: pioneerCc(ch, 0x00), label: `${u} BEATS rotate` },
		{ source: pioneerCc(ch, 0x10), label: `${u} BEATS rotate + SHIFT` },
		{ source: pioneerNote(ch, 0x43), label: `${u} BEATS press (TAP)` },
		{ source: pioneerNote(ch, 0x40), label: `${u} BEATS press + SHIFT (AUTO)` },
		// [PDF] 2-5..2-7 FX ON 1..3: notes 71/72/73 (0x47..0x49), +SHIFT 99/100/101 (0x63..0x65) = FX SELECT.
		{ source: pioneerNote(ch, 0x47), label: `${u}-1 ON` },
		{ source: pioneerNote(ch, 0x63), label: `${u}-1 ON + SHIFT (FX SELECT)` },
		{ source: pioneerNote(ch, 0x48), label: `${u}-2 ON` },
		{ source: pioneerNote(ch, 0x64), label: `${u}-2 ON + SHIFT (FX SELECT)` },
		{ source: pioneerNote(ch, 0x49), label: `${u}-3 ON` },
		{ source: pioneerNote(ch, 0x65), label: `${u}-3 ON + SHIFT (FX SELECT)` },
		// [PDF] 2-8 / 2-9 FX Assign (CH ASSIGN) on channel 7: notes base+0 / base+1, +SHIFT base+2 / base+3, long press 0x5A / 0x5B.
		{ source: pioneerNote(GLOBAL_CH, assignBase), label: `${u} assign CH${unit === 1 ? 1 : 2}` },
		{
			source: pioneerNote(GLOBAL_CH, assignBase + 1),
			label: `${u} assign CH${unit === 1 ? 2 : 4}`
		},
		{
			source: pioneerNote(GLOBAL_CH, assignBase + 2),
			label: `${u} assign CH${unit === 1 ? 1 : 2} + SHIFT (CH${unit === 1 ? 3 : 2})`
		},
		{
			source: pioneerNote(GLOBAL_CH, assignBase + 3),
			label: `${u} assign CH${unit === 1 ? 2 : 4} + SHIFT (CH${unit === 1 ? 4 : 4})`
		},
		{ source: pioneerNote(GLOBAL_CH, unit === 1 ? 0x5a : 0x5b), label: `${u} assign long press` }
	];
}

function _globalHints(): ControlHint[] {
	const ch = GLOBAL_CH;
	return [
		// [PDF] 4-1 BROWSE press: note 65 (0x41), +SHIFT 66 (0x42).
		{ source: pioneerNote(ch, 0x41), label: 'BROWSE press' },
		{ source: pioneerNote(ch, 0x42), label: 'BROWSE press + SHIFT' },
		// [PDF] 4-2 LOAD + SHIFT (INST. DOUBLES): deck 1..4 notes 88 (0x58), 89 (0x59), 96 (0x60), 97 (0x61).
		{ source: pioneerNote(ch, 0x58), label: 'LOAD deck 1 + SHIFT (INST. DOUBLES)' },
		{ source: pioneerNote(ch, 0x59), label: 'LOAD deck 2 + SHIFT (INST. DOUBLES)' },
		{ source: pioneerNote(ch, 0x60), label: 'LOAD deck 3 + SHIFT (INST. DOUBLES)' },
		{ source: pioneerNote(ch, 0x61), label: 'LOAD deck 4 + SHIFT (INST. DOUBLES)' },
		// [PDF] 4-3 BACK: note 101 (0x65), +SHIFT 102 (0x66). [PDF] 4-4 LOAD PREPARE: 103 (0x67), +SHIFT 104 (0x68).
		{ source: pioneerNote(ch, 0x65), label: 'BACK' },
		{ source: pioneerNote(ch, 0x66), label: 'BACK + SHIFT (VIEW)' },
		{ source: pioneerNote(ch, 0x67), label: 'LOAD PREPARE' },
		{ source: pioneerNote(ch, 0x68), label: 'LOAD PREPARE + SHIFT (AREA)' },
		// [PDF] 4-5 USB A: 105 (0x69), +SHIFT 106 (0x6A). [PDF] 4-6 USB B: 107 (0x6B), +SHIFT 108 (0x6C).
		{ source: pioneerNote(ch, 0x69), label: 'USB A' },
		{ source: pioneerNote(ch, 0x6a), label: 'USB A + SHIFT' },
		{ source: pioneerNote(ch, 0x6b), label: 'USB B' },
		{ source: pioneerNote(ch, 0x6c), label: 'USB B + SHIFT' },
		// [PDF] 3-14..3-17 SOUND COLOR FX: ECHO 0 (0x00), FILTER 1 (0x01), NOISE 2 (0x02), JET 3 (0x03); +SHIFT 8..11.
		{ source: pioneerNote(ch, 0x00), label: 'SOUND COLOR FX ECHO' },
		{ source: pioneerNote(ch, 0x08), label: 'SOUND COLOR FX ECHO + SHIFT' },
		{ source: pioneerNote(ch, 0x01), label: 'SOUND COLOR FX FILTER' },
		{ source: pioneerNote(ch, 0x09), label: 'SOUND COLOR FX FILTER + SHIFT' },
		{ source: pioneerNote(ch, 0x02), label: 'SOUND COLOR FX NOISE' },
		{ source: pioneerNote(ch, 0x0a), label: 'SOUND COLOR FX NOISE + SHIFT' },
		{ source: pioneerNote(ch, 0x03), label: 'SOUND COLOR FX JET' },
		{ source: pioneerNote(ch, 0x0b), label: 'SOUND COLOR FX JET + SHIFT' },
		// [PDF] 3-18 MASTER CUE + SHIFT: note 98 (0x62).
		{ source: pioneerNote(ch, 0x62), label: 'MASTER CUE + SHIFT' },
		// [PDF] 3-20 SAMPLER VOLUME: CC 3 (0x03) MSB, +SHIFT CC 105 (0x69).
		{ source: pioneerCc(ch, 0x03), label: 'SAMPLER VOLUME' },
		{ source: pioneerCc(ch, 0x69), label: 'SAMPLER VOLUME + SHIFT' }
	];
}

// -------------------------------------------------------------- led rules

function _ledRules(deck: DeckId): LedRule[] {
	const ch = pioneerDeckChannel(deck);
	const padCh = sx3PadChannel(deck);
	const on = 0x7f;
	const off = 0x00;
	return [
		// [PDF] 1-1 PLAY/PAUSE MIDI-OUT note 11; 1-2 CUE note 12 (lit when loaded); 1-3 SYNC note 93;
		// 1-21 AUTO LOOP note 20 (lit while a loop is engaged); 3-7 HEADPHONES CUE note 84.
		{
			trigger: { kind: 'deck_playing', deck },
			out: { ch, note: 0x0b, velocityOn: on, velocityOff: off }
		},
		{
			trigger: { kind: 'deck_loaded', deck },
			out: { ch, note: 0x0c, velocityOn: on, velocityOff: off }
		},
		{
			trigger: { kind: 'beat_sync_enabled', deck },
			out: { ch, note: 0x5d, velocityOn: on, velocityOff: off }
		},
		{
			trigger: { kind: 'loop_engaged', deck },
			out: { ch, note: 0x14, velocityOn: on, velocityOff: off }
		},
		{
			trigger: { kind: 'channel_cue_enabled', deck },
			out: { ch, note: 0x54, velocityOn: on, velocityOff: off }
		},
		// [PDF] 5-1 HOT CUE mode note 27, 5-4 SAMPLER mode note 34: lit while that mode is selected.
		{
			trigger: { kind: 'pad_mode_selected', deck, mode: 'hot_cue' },
			out: { ch, note: 0x1b, velocityOn: on, velocityOff: off }
		},
		{
			trigger: { kind: 'pad_mode_selected', deck, mode: 'sampler' },
			out: { ch, note: 0x22, velocityOn: on, velocityOff: off }
		},
		// [PDF] 5-5..5-12 pad MIDI-OUT: note = pad note, velocity = colour number 1-64.
		...HOT_CUE_SLOTS.map((slot, pad): LedRule => ({
			trigger: { kind: 'hot_cue_present', deck, slot },
			out: { ch: padCh, note: pad, velocityOn: SX3_PAD_COLOR_ON, velocityOff: SX3_PAD_COLOR_OFF }
		}))
	];
}

// ------------------------------------------------------------- device map

export const SX3_MAP: DeviceMap = {
	vendor: 'Pioneer DJ',
	// WebMIDI port names carry the product name, e.g. "DDJ-SX3".
	nameMatch: 'DDJ-SX3',
	bindings: [
		...DECKS.flatMap((deck) => _deckBindings(deck)),
		...DECKS.flatMap((deck) => _mixerBindings(deck)),
		...DECKS.flatMap((deck) => _padBindings(deck)),
		..._globalBindings()
	],
	leds: DECKS.flatMap((deck) => _ledRules(deck)),
	hints: [
		...DECKS.flatMap((deck) => _deckHints(deck)),
		...DECKS.flatMap((deck) => _padHints(deck)),
		..._fxHints(1),
		..._fxHints(2),
		..._globalHints()
	]
};

/** Self-registration: maps/entries.ts discovers this export, so no shared list names this map. */
export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{ key: 'SX3_MAP', map: SX3_MAP, live: true }
];
