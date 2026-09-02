/**
 * Pioneer DJ DDJ-400 P0 device map (build unit: DDJ-400 map).
 *
 * SOURCE OF TRUTH for EVERY Note/CC number in this file: the official
 * Pioneer DJ "DDJ-400 List of MIDI messages" PDF (version 1.00, E1),
 * from Pioneer's DDJ-400_MIDI_Message_List_E1.pdf (not redistributed here;
 * see docs/controller/reference/README.md for how to obtain it)
 * - cited throughout as [PDF] with the PDF's own Fig. row ids (D1, M3, B1,
 * P1..) and printed page numbers (p.1 BROWSER+DECK, p.2 MIXER+EFFECT+PAD1-4,
 * p.3 PAD5-8).
 *
 * DO NOT source numbers from tools/deck-diagrams/devices/ddj-400/midi.json -
 * that file is `"bootstrap": true` plate-label stubs with sequential invented
 * codes, not transcribed wire data.
 *
 * Channel layout ([PDF] p.1 "MIDI channel assignment" table):
 *   deck 1 / deck 2 (non-pad)    -> channels 1 / 2
 *   EFFECT                       -> channel 5 (out of P0 scope)
 *   BROWSER + global mixer       -> channel 7
 *   performance pads, unshifted  -> channels 8 (deck 1) / 10 (deck 2)
 *   performance pads, shifted    -> channels 9 / 11 (out of P0 scope)
 *
 * SHIFT NOTE: like the FLX10 the DDJ-400 encodes shift in HARDWARE - a
 * shifted control sends a DIFFERENT data1 (PLAY sends note 71 not 11,
 * [PDF] p.1 D1) and pads move to their own channels. So this map needs no
 * `shift: true` bindings; the SHIFT button itself binds shift_modifier only
 * so midiState.shiftHeld stays truthful for UI display.
 *
 * 14-BIT MIXER CAVEAT (identical to the FLX10 map): every DDJ-400 mixer
 * control is an MSB/LSB CC pair ([PDF] p.2 M1-M8). The P0 contract pairs
 * 14-bit CCs for deck_pitch ONLY, so mixer controls bind their MSB CC and
 * the LSB CCs (36/39/43/47/51 on ch 1-2, 63/40 on ch 7) land in the learn
 * log as unmapped. Loud by design.
 *
 * TWO DECKS, NOT FOUR: the DDJ-400 is a 2-channel controller. Decks 3/4
 * exist in the engine but no DDJ-400 control addresses them - drive those
 * from the UI. Nothing here fabricates a deck-3/4 binding.
 *
 * OUT OF CONTRACT (numbers preserved as hints, NOT bound): jog dials
 * ([PDF] p.1 D3 - no scratch/jog action in the P0 union), BEAT SYNC (D5),
 * LOOP IN / LOOP OUT (D6/D7), CUE/LOOP CALL (D9/D10), CH CUE (M7),
 * HEADPHONES MIXING/LEVEL (M10/M11), MASTER CUE (M9), FILTER (F1),
 * BEAT FX (F2-F7).
 *
 * Requirements (mini-PRD):
 *   ✔︎ Transport play/cue per deck ([PDF] p.1 D1/D2).
 *     [if] note 0x0B ch 1 arrives [then] deck_play_toggle deck 1
 *   ✔︎ 16 hot-cue pads: notes 0-7 x pad channels 8/10 -> slots A-H
 *     ([PDF] p.2-3 P1-P8 HOT CUE mode).
 *     [if] note 3 ch 10 arrives [then] deck_hot_cue deck 2 slot D
 *   ✔︎ 16 beat-loop pads: notes 96-103 -> beats table ([PDF] p.2-3 P1-P8
 *     BEAT LOOP mode); 4 BEAT long-press ([PDF] p.1 D6) + RELOOP/EXIT (D8).
 *     [if] note 0x4D ch 2 arrives [then] deck_loop_exit deck 2
 *   ✔︎ Mixer per channel (trim/EQ/fader) + crossfader + master on MSB CCs
 *     ([PDF] p.2 M1-M8); browse encoder relative + LOAD 1-2 ([PDF] p.1 B1/B2).
 *   ✔︎ Tempo fader as true 14-bit MSB/LSB pair via deck_pitch lsbOffset 32
 *     ([PDF] p.1 D4: CC 0 MSB / CC 32 LSB).
 *     [if] MSB then LSB arrive on ch 2 CC 0/32 [then ⛔️] anything but ONE
 *       continuous14 deck_pitch emit for deck 2 is broken
 *   ✔︎ LED rules only where the [PDF] prints a MIDI-OUT column for that row
 *     ("← Same as MIDI-IN"): PLAY, CUE, RELOOP/EXIT, pads. The 4 BEAT
 *     long-press row (note 20) prints NO MIDI-OUT, so it drives no LED.
 *     [if] a rule is added for a row with no MIDI-OUT column [then ⛔️] it is
 *       fabricated feedback
 *   ✔︎ Zero fabricated numbers: each block comments its [PDF] Fig. row.
 */

import type { ControlHint, DeviceMap, LedRule, MidiBinding } from '$lib/rb/midi/midi-types';
import type { DeckId } from '$lib/rb/deck-slots';
import type { HotCueSlot } from '$lib/rb/hot-cue-types';

// -------------------------------------------------------------- constants

/** The DDJ-400 addresses decks 1 and 2 only (2-channel unit). */
const DECKS: readonly DeckId[] = [1, 2];

/** Hot-cue pad order: pad k triggers slot HOT_CUE_SLOTS[k-1]. Wire fact:
 * pad k sends note k-1 ([PDF] p.2-3, P1..P8 HOT CUE mode rows: notes
 * 0,1,2,3,4,5,6,7). Slot lettering is our engine's A-H convention. */
const HOT_CUE_SLOTS: readonly HotCueSlot[] = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];

/** Beats per BEAT LOOP pad 1..8. The [PDF] documents only the wire notes
 * (96-103); loop LENGTH per pad is software-side (rekordbox assigns its
 * own), so this table is OUR software assignment - same rekordbox-style
 * doubling ladder the FLX10 map uses, so muscle memory carries across. */
export const DDJ400_BEAT_LOOP_PAD_BEATS: readonly number[] = [0.25, 0.5, 1, 2, 4, 8, 16, 32];

/** Pad LED on/off velocities. Unlike the FLX10's RGB pads (colour number
 * 1-127), the [PDF] pad MIDI-OUT rows print a plain "OFF=0x00, ON=0x7F" -
 * the DDJ-400's pads are single-colour, so there is no palette to resolve
 * and no per-cue colour to fabricate. */
export const DDJ400_PAD_ON = 0x7f;
export const DDJ400_PAD_OFF = 0x00;

// ---------------------------------------------------------------- _helpers

/** Deck n non-pad controls live on channel n ([PDF] p.1 channel table). */
function _deckCh(deck: DeckId): number {
	return deck;
}

/** Unshifted performance pads: deck 1/2 -> channel 8/10 ([PDF] p.1 channel
 * table: "DECK 1 PERFORMANCE PAD (without SHIFT)" = 8, deck 2 = 10). */
function _padCh(deck: DeckId): number {
	return 6 + 2 * deck;
}

function _note(ch: number, id: number): MidiBinding['source'] {
	return { ch, kind: 'note', id };
}

function _cc(ch: number, id: number): MidiBinding['source'] {
	return { ch, kind: 'cc', id };
}

// ---------------------------------------------------- per-deck deck section

function _deckBindings(deck: DeckId): MidiBinding[] {
	const ch = _deckCh(deck);
	return [
		// [PDF] p.1 D1 PLAY/PAUSE: 9n note 11 (0x0B).
		{ source: _note(ch, 0x0b), action: { type: 'deck_play_toggle', deck } },
		// [PDF] p.1 D2 CUE: 9n note 12 (0x0C).
		{ source: _note(ch, 0x0c), action: { type: 'deck_cue', deck } },
		// [PDF] p.1 D11 SHIFT: 9n note 63 (0x3F).
		{ source: _note(ch, 0x3f), action: { type: 'shift_modifier' } },
		// [PDF] p.1 D4 TEMPO: CC 0 MSB / CC 32 (0x20) LSB, "-" side = Min,
		// "+" side = Max. Glue convention is 0 -> -range, 1 -> +range, so
		// the wire orientation matches directly (no invert).
		{ source: _cc(ch, 0x00), action: { type: 'deck_pitch', deck, lsbOffset: 32 } },
		// [PDF] p.1 D6 "LOOP IN/4BEAT", LONG press: 9n note 20 (0x14), data
		// fixed 0x7F. Long-press is the hardware's own 4-beat auto loop, so
		// this is the DDJ-400's one-button loop. Beats=4 is [PDF]-labelled
		// ("4BEAT"), not a software choice.
		{ source: _note(ch, 0x14), action: { type: 'deck_beat_loop', deck, beats: 4 } },
		// [PDF] p.1 D8 RELOOP/EXIT: 9n note 77 (0x4D). Software semantics
		// (ours): exit the active loop.
		{ source: _note(ch, 0x4d), action: { type: 'deck_loop_exit', deck } }
		// NOT BOUND (P0 contract has no loop_in/loop_out/sync/jog actions) -
		// see _deckHints for the cited wire numbers.
	];
}

// -------------------------------------------------- per-deck mixer section

function _mixerBindings(deck: DeckId): MidiBinding[] {
	const ch = _deckCh(deck);
	return [
		// [PDF] p.2 M3 TRIM: CC 4 (0x04) MSB (LSB 36 unbound - header note).
		{ source: _cc(ch, 0x04), action: { type: 'mixer_channel', deck, target: 'trim' } },
		// [PDF] p.2 M4 EQ HI: CC 7 (0x07) MSB (LSB 39 unbound).
		{ source: _cc(ch, 0x07), action: { type: 'mixer_channel', deck, target: 'eq', band: 'high' } },
		// [PDF] p.2 M5 EQ MID: CC 11 (0x0B) MSB (LSB 43 unbound).
		{ source: _cc(ch, 0x0b), action: { type: 'mixer_channel', deck, target: 'eq', band: 'mid' } },
		// [PDF] p.2 M6 EQ LOW: CC 15 (0x0F) MSB (LSB 47 unbound).
		{ source: _cc(ch, 0x0f), action: { type: 'mixer_channel', deck, target: 'eq', band: 'low' } },
		// [PDF] p.2 M2 CH FADER: CC 19 (0x13) MSB (LSB 51 unbound), "Min at
		// bottom end, Max at top end" - matches setFader 0..1, no invert.
		{ source: _cc(ch, 0x13), action: { type: 'mixer_channel', deck, target: 'fader' } }
	];
}

// -------------------------------------------------------- per-deck pad bank

function _padBindings(deck: DeckId): MidiBinding[] {
	const ch = _padCh(deck);
	const bindings: MidiBinding[] = [];
	for (let pad = 0; pad < 8; pad++) {
		// [PDF] p.2-3 P1..P8, HOT CUE mode: pad k -> note k-1 (0..7).
		bindings.push({
			source: _note(ch, pad),
			action: { type: 'deck_hot_cue', deck, slot: HOT_CUE_SLOTS[pad] }
		});
		// [PDF] p.2-3 P1..P8, BEAT LOOP mode: pad k -> note 95+k
		// (96..103 / 0x60..0x67). Beats per pad: our table (see const).
		bindings.push({
			source: _note(ch, 0x60 + pad),
			action: { type: 'deck_beat_loop', deck, beats: DDJ400_BEAT_LOOP_PAD_BEATS[pad] }
		});
	}
	return bindings;
}

// -------------------------------------------------- browser + global mixer

function _browserAndGlobalBindings(): MidiBinding[] {
	// Browser + global mixer share channel 7 ([PDF] p.1 channel table).
	const ch = 7;
	return [
		// [PDF] p.1 B1 Rotary Selector rotate: CC 64 (0x40), relative ticks
		// ("Turn clockwise : 1 ~ 30 (0x01 ~ 0x1E), counterclockwise :
		// 127 ~ 98 (0x7F ~ 0x62)").
		{ source: _cc(ch, 0x40), action: { type: 'browse_encoder' }, relative: true },
		// [PDF] p.1 B1 rotate +SHIFT: CC 100 (0x64), same tick encoding.
		// Software choice (ours): same selection movement.
		{ source: _cc(ch, 0x64), action: { type: 'browse_encoder' }, relative: true },
		// [PDF] p.1 B2-L / B2-R LOAD: deck 1 note 70 (0x46), deck 2 note 71
		// (0x47). The DDJ-400 has one LOAD button per deck (no deck 3/4).
		{ source: _note(ch, 0x46), action: { type: 'browse_load', deck: 1 } },
		{ source: _note(ch, 0x47), action: { type: 'browse_load', deck: 2 } },
		// [PDF] p.2 M1 CROSSFADER: CC 31 (0x1F) MSB (LSB 63 unbound),
		// "Min at left side, Max at right side" - engine x=0 is full A
		// (left), no invert.
		{ source: _cc(ch, 0x1f), action: { type: 'mixer_global', target: 'crossfader' } },
		// [PDF] p.2 M8 MASTER LEVEL: CC 8 (0x08) MSB (LSB 40 unbound).
		{ source: _cc(ch, 0x08), action: { type: 'mixer_global', target: 'master' } }
	];
}

// ------------------------------------------------------------------- hints

/** Documented-but-unbound physical controls. The P0 MidiAction union has no
 * case for any of these, so pressing them produces learn-log traffic only -
 * the hint names what was touched instead of showing a bare "unmapped
 * source". Every number is transcribed from the [PDF] rows cited inline. */
function _deckHints(deck: DeckId): ControlHint[] {
	const ch = _deckCh(deck);
	return [
		// [PDF] p.1 D3 JOG DIAL: platter rotate CC 34 (0x22) vinyl-mode ON /
		// CC 35 (0x23) vinyl-mode OFF, +SHIFT CC 41 (0x29); wheel-side rotate
		// CC 33 (0x21); touch note 54 (0x36), +SHIFT note 103 (0x67).
		{ source: _cc(ch, 0x22), label: `JOG platter, vinyl on (deck ${deck})` },
		{ source: _cc(ch, 0x23), label: `JOG platter, vinyl off (deck ${deck})` },
		{ source: _cc(ch, 0x29), label: `JOG platter + SHIFT (deck ${deck})` },
		{ source: _cc(ch, 0x21), label: `JOG wheel side (deck ${deck})` },
		{ source: _note(ch, 0x36), label: `JOG touch (deck ${deck})` },
		{ source: _note(ch, 0x67), label: `JOG touch + SHIFT (deck ${deck})` },
		// [PDF] p.1 D5 BEAT SYNC: note 88 (0x58), LONG press note 92 (0x5C),
		// +SHIFT note 96 (0x60).
		{ source: _note(ch, 0x58), label: `BEAT SYNC (deck ${deck})` },
		{ source: _note(ch, 0x5c), label: `BEAT SYNC long press (deck ${deck})` },
		{ source: _note(ch, 0x60), label: `BEAT SYNC + SHIFT (deck ${deck})` },
		// [PDF] p.1 D6 LOOP IN/4BEAT short press: note 16 (0x10), +SHIFT
		// note 76 (0x4C). (The LONG press, note 20, IS bound - see above.)
		{ source: _note(ch, 0x10), label: `LOOP IN (deck ${deck})` },
		{ source: _note(ch, 0x4c), label: `LOOP IN + SHIFT (deck ${deck})` },
		// [PDF] p.1 D7 LOOP OUT: note 17 (0x11), +SHIFT note 78 (0x4E).
		{ source: _note(ch, 0x11), label: `LOOP OUT (deck ${deck})` },
		{ source: _note(ch, 0x4e), label: `LOOP OUT + SHIFT (deck ${deck})` },
		// [PDF] p.1 D8 RELOOP/EXIT +SHIFT: note 80 (0x50). (Unshifted IS bound.)
		{ source: _note(ch, 0x50), label: `RELOOP/EXIT + SHIFT (deck ${deck})` },
		// [PDF] p.1 D9 CUE/LOOP CALL left: note 81 (0x51), +SHIFT note 62 (0x3E).
		{ source: _note(ch, 0x51), label: `CUE/LOOP CALL back (deck ${deck})` },
		{ source: _note(ch, 0x3e), label: `CUE/LOOP CALL back + SHIFT (deck ${deck})` },
		// [PDF] p.1 D10 CUE/LOOP CALL right: note 83 (0x53), +SHIFT note 61 (0x3D).
		{ source: _note(ch, 0x53), label: `CUE/LOOP CALL forward (deck ${deck})` },
		{ source: _note(ch, 0x3d), label: `CUE/LOOP CALL forward + SHIFT (deck ${deck})` },
		// [PDF] p.2 M7 CH CUE (headphone PFL): note 84 (0x54), +SHIFT note
		// 104 (0x68). Unbound: the P0 union has no channel-cue action.
		{ source: _note(ch, 0x54), label: `CH CUE / headphone (deck ${deck})` },
		{ source: _note(ch, 0x68), label: `CH CUE + SHIFT (deck ${deck})` }
	];
}

/** Global (channel 5 EFFECT / channel 7 BROWSER+MIXER) unbound controls. */
function _globalHints(): ControlHint[] {
	const fx = 5;
	const ch = 7;
	return [
		// [PDF] p.1 B1 Rotary Selector press: note 65 (0x41), +SHIFT note 66 (0x42).
		{ source: _note(ch, 0x41), label: 'BROWSE press' },
		{ source: _note(ch, 0x42), label: 'BROWSE press + SHIFT' },
		// [PDF] p.1 B2-L / B2-R LOAD +SHIFT: note 104 (0x68) / note 122 (0x7A).
		{ source: _note(ch, 0x68), label: 'LOAD deck 1 + SHIFT' },
		{ source: _note(ch, 0x7a), label: 'LOAD deck 2 + SHIFT' },
		// [PDF] p.2 M9 MASTER CUE: note 99 (0x63), +SHIFT note 120 (0x78).
		{ source: _note(ch, 0x63), label: 'MASTER CUE' },
		{ source: _note(ch, 0x78), label: 'MASTER CUE + SHIFT' },
		// [PDF] p.2 M10 HEADPHONES MIXING: CC 12 (0x0C) MSB / 44 LSB.
		{ source: _cc(ch, 0x0c), label: 'HEADPHONES MIXING' },
		// [PDF] p.2 M11 HEADPHONES LEVEL: CC 13 (0x0D) MSB / 45 LSB.
		{ source: _cc(ch, 0x0d), label: 'HEADPHONES LEVEL' },
		// [PDF] p.2 F1-1 / F1-2 FILTER: CC 23 (0x17) / CC 24 (0x18) MSB.
		{ source: _cc(ch, 0x17), label: 'FILTER (deck 1)' },
		{ source: _cc(ch, 0x18), label: 'FILTER (deck 2)' },
		// [PDF] p.2 F2/F3 BEAT left/right: ch 5 note 74 (0x4A) / 75 (0x4B).
		{ source: _note(fx, 0x4a), label: 'BEAT FX beat back' },
		{ source: _note(fx, 0x4b), label: 'BEAT FX beat forward' },
		// [PDF] p.2 F4 BEAT FX SELECT: ch 5 note 99 (0x63).
		{ source: _note(fx, 0x63), label: 'BEAT FX SELECT' },
		// [PDF] p.2 F6 BEAT FX LEVEL/DEPTH: ch 5 CC 2 (0x02) MSB.
		{ source: _cc(fx, 0x02), label: 'BEAT FX LEVEL/DEPTH' },
		// [PDF] p.2 F7 BEAT FX ON/OFF: ch 5 note 71 (0x47), +SHIFT note 67 (0x43).
		{ source: _note(fx, 0x47), label: 'BEAT FX ON/OFF' },
		{ source: _note(fx, 0x43), label: 'BEAT FX ON/OFF + SHIFT' }
	];
}

// -------------------------------------------------------------- led rules

/** LED rules exist ONLY for rows whose [PDF] MIDI-OUT column prints
 * "← Same as MIDI-IN" - anything else would be invented feedback. */
function _ledRules(deck: DeckId): LedRule[] {
	const ch = _deckCh(deck);
	const padCh = _padCh(deck);
	const rules: LedRule[] = [
		// [PDF] p.1 D1 PLAY/PAUSE MIDI-OUT: 9n note 11 (0x0B), OFF=0x00
		// ON=0x7F. Lit while the deck plays.
		{
			trigger: { kind: 'deck_playing', deck },
			out: { ch, note: 0x0b, velocityOn: 0x7f, velocityOff: 0x00 }
		},
		// [PDF] p.1 D2 CUE MIDI-OUT: 9n note 12 (0x0C). Software choice
		// (ours): lit when a track is loaded (a cue point exists to return to).
		{
			trigger: { kind: 'deck_loaded', deck },
			out: { ch, note: 0x0c, velocityOn: 0x7f, velocityOff: 0x00 }
		},
		// [PDF] p.1 D8 RELOOP/EXIT MIDI-OUT: 9n note 77 (0x4D). Lit while a
		// loop is engaged. NOTE: the 4 BEAT long-press row (note 20) prints
		// no MIDI-OUT column, so loop state is shown on RELOOP/EXIT instead.
		{
			trigger: { kind: 'loop_engaged', deck },
			out: { ch, note: 0x4d, velocityOn: 0x7f, velocityOff: 0x00 }
		}
	];
	for (let pad = 0; pad < 8; pad++) {
		// [PDF] p.2-3 pad MIDI-OUT: 9p note = pad note, OFF=0x00 ON=0x7F
		// (single-colour pads - no palette). Lit when the slot is populated.
		rules.push({
			trigger: { kind: 'hot_cue_present', deck, slot: HOT_CUE_SLOTS[pad] },
			out: { ch: padCh, note: pad, velocityOn: DDJ400_PAD_ON, velocityOff: DDJ400_PAD_OFF }
		});
	}
	return rules;
}

// ------------------------------------------------------------- device map

export const DDJ400_MAP: DeviceMap = {
	vendor: 'Pioneer DJ',
	// WebMIDI port names carry the product name, e.g. "DDJ-400".
	nameMatch: 'DDJ-400',
	bindings: [
		...DECKS.flatMap((deck) => _deckBindings(deck)),
		...DECKS.flatMap((deck) => _mixerBindings(deck)),
		...DECKS.flatMap((deck) => _padBindings(deck)),
		..._browserAndGlobalBindings()
	],
	leds: DECKS.flatMap((deck) => _ledRules(deck)),
	hints: [...DECKS.flatMap((deck) => _deckHints(deck)), ..._globalHints()]
};
