/**
 * Pioneer DJ DDJ-FLX6 P0 device map (build unit: DDJ-FLX6 practice replica, epic #9366).
 *
 * SOURCE OF TRUTH for EVERY Note/CC number in this file: the official AlphaTheta
 * "DDJ-FLX6 List of MIDI messages" PDF (E1, 2020), from
 * https://downloads.support.alphatheta.com/software_info/dj-controllers/DDJ-FLX6/DDJ-FLX6_MIDI_message_List_E1.pdf
 * (not redistributed here; sha256 recorded in
 * docs/controller/research/DDJ-FLX6-MIDI-Message-List.md), cited as [PDF] with
 * the sheet's own Fig. ids (1-1 .. 5-5, L/R suffix = left/right deck strip).
 *
 * DO NOT source numbers from tools/deck-diagrams/devices/ddj-flx6/midi.json -
 * that file is a plate-label stub from the older diagram effort, not the map.
 *
 * Channel layout ([PDF] MIDI channel column):
 *   decks 1-4 (non-pad)            -> channels 1-4 (the left strip is deck 1 or 3,
 *                                     the right strip deck 2 or 4; DECK toggles)
 *   MERGE FX left / right          -> channels 5 / 6 (same channels as Beat FX bank 1 / 2)
 *   BROWSER + global mixer + FILTER-> channel 7
 *   performance pads, unshifted    -> channels 8/10/12/14 (deck 1/2/3/4)
 *   performance pads, shifted      -> channels 9/11/13/15
 *   jog / MERGE FX illumination    -> MIDI-OUT only (channel 12, 5/6), not driven here
 *
 * SHIFT NOTE: the FLX6 encodes shift in HARDWARE - a shifted control sends a
 * different data1 and pads move to their own channels. This map binds
 * shift_modifier only so midiState.shiftHeld stays truthful.
 *
 * 14-BIT MIXER CAVEAT: mixer controls bind MSB CC only; LSB CCs land in the
 * learn log as unmapped. True 14-bit pairing is deck_pitch only.
 *
 * FOUR DECKS on two strips: like the FLX10 map every deck 1-4 is bound, so the
 * DECK button's channel switch on the hardware just works. The practice panel
 * carries the deck 1 / deck 2 wire of each strip.
 *
 * FILTER ([PDF] 3-7) is CC 23/24/25/26 for CH1-4 on the GLOBAL channel 7, the
 * same layout the FLX4 hardware confirmed Fri 18 Sep 2026.
 *
 * FX: the per-channel FILTER knob is the FLX6's colour FX and is bound
 * (mixer_channel filter). Beat FX and MERGE FX stay hints: the P0 MidiAction
 * union has no beat-FX action yet (follow-up #9788).
 *
 * No hardware capture: every binding is inferred from the [PDF] (hardware not confirmed).
 *
 * OUT OF CONTRACT (hints only): jog touch / + SHIFT twins, JOG CUTTER, DECK select, IN/OUT press,
 * CUE/LOOP CALL and MEMORY, MERGE FX, Beat FX, pad-mode buttons, shifted pads,
 * BACK / VIEW, INPUT SELECT, BOOTH LEVEL, MIC LEVEL, every + SHIFT twin.
 */

import type {
	ControlHint,
	DeviceMap,
	DeviceMapEntry,
	LedRule,
	MidiBinding,
	MidiMeterOutput
} from '$lib/rb/midi/midi-types';
import type { DeckId } from '$lib/rb/deck-slots';
import {
	pioneerCc,
	pioneerDeckBindings,
	pioneerDeckChannel,
	pioneerNote,
	pioneerPadBindings,
	pioneerPadChannel,
	pioneerPadLedRules,
	pioneerTransportLedRules
} from './pioneer-deck-bindings';

const DECKS: readonly DeckId[] = [1, 2, 3, 4];

export const FLX6_BEAT_LOOP_PAD_BEATS: readonly number[] = [0.25, 0.5, 1, 2, 4, 8, 16, 32];
export const FLX6_PAD_ON = 0x7f;
export const FLX6_PAD_OFF = 0x00;

/** [PDF] channel column: BROWSER + global mixer (incl. FILTER) -> channel 7. */
const MIXER_GLOBAL_CH = 7;
/** [PDF] 3-7 FILTER (CH1..CH4): CC 23..26 (0x17..0x1A) MSB on channel 7. */
const FILTER_CC: Record<DeckId, number> = { 1: 0x17, 2: 0x18, 3: 0x19, 4: 0x1a };
/** [PDF] 4-4 .. 4-7 LOAD (Deck1..4): notes 70..73 (0x46..0x49) on channel 7. */
const LOAD_NOTE: Record<DeckId, number> = { 1: 0x46, 2: 0x47, 3: 0x48, 4: 0x49 };
/** [PDF] 4-4 .. 4-7 LOAD + SHIFT: notes 0x58 / 0x59 / 0x60 / 0x61. */
const LOAD_SHIFT_NOTE: Record<DeckId, number> = { 1: 0x58, 2: 0x59, 3: 0x60, 4: 0x61 };

function _deckBindings(deck: DeckId): MidiBinding[] {
	const ch = pioneerDeckChannel(deck);
	const jog = (cc: number): MidiBinding => ({
		source: pioneerCc(ch, cc),
		action: { type: 'deck_jog', deck },
		relative: true,
		relativeCentered: true
	});
	return [
		// [PDF] 1-1 PLAY/PAUSE 0x0B, 1-2 CUE 0x0C, 1-3 SHIFT 0x3F, 1-17 TEMPO CC 0/32,
		// 1-7 IN/-4BEAT long press 0x14 (4-beat loop), 1-9 RELOOP/EXIT 0x4D.
		...pioneerDeckBindings(deck, 0x4d),
		// [PDF] 1-15 BEAT SYNC: note 88 (0x58).
		{ source: pioneerNote(ch, 0x58), action: { type: 'deck_sync_toggle', deck } },
		// [PDF] 1-4 JOG DIAL: platter CC 34 (vinyl on), 35 (vinyl off), wheel side CC 33.
		jog(0x22),
		jog(0x23),
		jog(0x21)
	];
}

function _mixerBindings(deck: DeckId): MidiBinding[] {
	const ch = pioneerDeckChannel(deck);
	return [
		// [PDF] 3-5 TRIM: CC 4 MSB.
		{ source: pioneerCc(ch, 0x04), action: { type: 'mixer_channel', deck, target: 'trim' } },
		// [PDF] 3-6 EQ Hi / Mid / Low: CC 7 / 11 / 15 MSB.
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
		// [PDF] 3-9 CH FADER: CC 19 MSB.
		{ source: pioneerCc(ch, 0x13), action: { type: 'mixer_channel', deck, target: 'fader' } },
		// [PDF] 3-7 FILTER: channel 7 CC 23..26.
		{
			source: pioneerCc(MIXER_GLOBAL_CH, FILTER_CC[deck]),
			action: { type: 'mixer_channel', deck, target: 'filter' }
		},
		// [PDF] 3-8 CH CUE: note 84 (0x54).
		{ source: pioneerNote(ch, 0x54), action: { type: 'channel_cue', deck } }
	];
}

function _padBindings(deck: DeckId): MidiBinding[] {
	// [PDF] 5-5 HOT CUE mode notes 0-7 and BEAT LOOP mode notes 0x60-0x67 on the
	// deck's unshifted pad channel. The doubling ladder is our software assignment.
	return pioneerPadBindings(deck, FLX6_BEAT_LOOP_PAD_BEATS);
}

function _browserAndGlobalBindings(): MidiBinding[] {
	const ch = MIXER_GLOBAL_CH;
	return [
		// [PDF] 4-1 BROWSE turn: CC 64 (0x40), + SHIFT CC 100 (0x64), relative ticks.
		{ source: pioneerCc(ch, 0x40), action: { type: 'browse_encoder' }, relative: true },
		{ source: pioneerCc(ch, 0x64), action: { type: 'browse_encoder' }, relative: true },
		// [PDF] 4-4 .. 4-7 LOAD (Deck1..4).
		...DECKS.map((deck): MidiBinding => ({
			source: pioneerNote(ch, LOAD_NOTE[deck]),
			action: { type: 'browse_load', deck }
		})),
		// [PDF] 3-10 CROSSFADER: CC 31 (0x1F) MSB.
		{ source: pioneerCc(ch, 0x1f), action: { type: 'mixer_global', target: 'crossfader' } },
		// [PDF] 3-2 MASTER LEVEL: CC 8 MSB.
		{ source: pioneerCc(ch, 0x08), action: { type: 'mixer_global', target: 'master' } },
		// [PDF] 3-13 HEADPHONE MIX CC 12, 3-12 HEADPHONE LEVEL CC 13 (front panel).
		{ source: pioneerCc(ch, 0x0c), action: { type: 'headphone_mix' } },
		{ source: pioneerCc(ch, 0x0d), action: { type: 'headphone_level' } },
		// [PDF] 3-3 MASTER CUE: note 99 (0x63).
		{ source: pioneerNote(ch, 0x63), action: { type: 'master_cue', mode: 'latch' } }
	];
}

/** Documented-but-unbound per-deck controls ([PDF] 1-1 .. 1-17, 3-8, 5-1 .. 5-5). */
function _deckHints(deck: DeckId): ControlHint[] {
	const ch = pioneerDeckChannel(deck);
	const padShiftCh = pioneerPadChannel(deck) + 1;
	const d = `(deck ${deck})`;
	const hints: ControlHint[] = [
		// [PDF] 1-1 / 1-2 shifted twins.
		{ source: pioneerNote(ch, 0x47), label: `PLAY/PAUSE + SHIFT ${d}` },
		{ source: pioneerNote(ch, 0x48), label: `CUE + SHIFT ${d}` },
		// [PDF] 1-4 JOG DIAL + SHIFT CC 41 / 38 and touch 54 / 103 (CC 33/34/35 are bound).
		{ source: pioneerCc(ch, 0x29), label: `JOG platter + SHIFT ${d}` },
		{ source: pioneerCc(ch, 0x26), label: `JOG wheel side + SHIFT ${d}` },
		{ source: pioneerNote(ch, 0x36), label: `JOG touch ${d}` },
		{ source: pioneerNote(ch, 0x67), label: `JOG touch + SHIFT ${d}` },
		// [PDF] 1-5 DECK: note 60 (0x3C) on the strip's channels; + SHIFT is on channel 7.
		{ source: pioneerNote(ch, 0x3c), label: `DECK select ${d}` },
		// [PDF] 1-6 JOG CUTTER: note 28 (0x1C), + SHIFT note 23 (0x17).
		{ source: pioneerNote(ch, 0x1c), label: `JOG CUTTER ${d}` },
		{ source: pioneerNote(ch, 0x17), label: `JOG CUTTER + SHIFT ${d}` },
		// [PDF] 1-7 IN/-4BEAT press 0x10 (+ SHIFT 0x4C); the long-press 0x14 row is bound.
		{ source: pioneerNote(ch, 0x10), label: `IN/-4BEAT ${d}` },
		{ source: pioneerNote(ch, 0x4c), label: `IN/-4BEAT + SHIFT ${d}` },
		// [PDF] 1-8 OUT 0x11 (+ SHIFT 0x77), 1-9 RELOOP/EXIT + SHIFT 0x50.
		{ source: pioneerNote(ch, 0x11), label: `OUT ${d}` },
		{ source: pioneerNote(ch, 0x77), label: `OUT + SHIFT ${d}` },
		{ source: pioneerNote(ch, 0x50), label: `RELOOP/EXIT + SHIFT ${d}` },
		// [PDF] 1-10 / 1-11 CUE/LOOP CALL, 1-12 CUE/LOOP MEMORY.
		{ source: pioneerNote(ch, 0x51), label: `CUE/LOOP CALL back ${d}` },
		{ source: pioneerNote(ch, 0x61), label: `CUE/LOOP CALL back + SHIFT ${d}` },
		{ source: pioneerNote(ch, 0x53), label: `CUE/LOOP CALL forward ${d}` },
		{ source: pioneerNote(ch, 0x62), label: `CUE/LOOP CALL forward + SHIFT ${d}` },
		{ source: pioneerNote(ch, 0x3d), label: `CUE/LOOP MEMORY ${d}` },
		{ source: pioneerNote(ch, 0x3e), label: `CUE/LOOP MEMORY + SHIFT ${d}` },
		// [PDF] 1-15 BEAT SYNC + SHIFT 0x5D, 1-16 MASTER 0x5C (+ SHIFT 0x60).
		{ source: pioneerNote(ch, 0x5d), label: `BEAT SYNC + SHIFT ${d}` },
		{ source: pioneerNote(ch, 0x5c), label: `MASTER (tempo master) ${d}` },
		{ source: pioneerNote(ch, 0x60), label: `MASTER + SHIFT ${d}` },
		// [PDF] 1-17 TEMPO + SHIFT: CC 5 MSB (tempo range).
		{ source: pioneerCc(ch, 0x05), label: `TEMPO + SHIFT ${d}` },
		// [PDF] 3-8 CH CUE + SHIFT: note 104 (0x68).
		{ source: pioneerNote(ch, 0x68), label: `CH CUE + SHIFT ${d}` },
		// [PDF] 5-1 .. 5-4 pad mode buttons and their shifted twins.
		{ source: pioneerNote(ch, 0x1b), label: `HOT CUE MODE ${d}` },
		{ source: pioneerNote(ch, 0x69), label: `HOT CUE MODE + SHIFT ${d}` },
		{ source: pioneerNote(ch, 0x1e), label: `PAD FX MODE ${d}` },
		{ source: pioneerNote(ch, 0x6b), label: `PAD FX MODE + SHIFT ${d}` },
		{ source: pioneerNote(ch, 0x20), label: `BEAT JUMP MODE ${d}` },
		{ source: pioneerNote(ch, 0x6d), label: `BEAT JUMP MODE + SHIFT ${d}` },
		{ source: pioneerNote(ch, 0x22), label: `SAMPLER MODE ${d}` },
		{ source: pioneerNote(ch, 0x6f), label: `SAMPLER MODE + SHIFT ${d}` }
	];
	// [PDF] 5-5 shifted pads: HOT CUE mode notes 0-7 on the odd pad channel.
	for (let pad = 0; pad < 8; pad++) {
		hints.push({ source: pioneerNote(padShiftCh, pad), label: `PAD ${pad + 1} + SHIFT ${d}` });
	}
	return hints;
}

/** [PDF] 1-13 / 1-14 MERGE FX and 2-1 .. 2-6 Beat FX, one bank per channel 5 / 6. */
function _fxHints(bank: 1 | 2): ControlHint[] {
	const ch = 4 + bank;
	const side = bank === 1 ? 'L' : 'R';
	return [
		{ source: pioneerNote(ch, 0x2f), label: `MERGE FX PRESET ${side}` },
		{ source: pioneerNote(ch, 0x30), label: `MERGE FX PRESET ${side} + SHIFT` },
		{ source: pioneerCc(ch, 0x08), label: `MERGE FX PARAMETER ${side} turn` },
		{ source: pioneerCc(ch, 0x28), label: `MERGE FX PARAMETER ${side} turn + SHIFT` },
		{ source: pioneerNote(ch, 0x2e), label: `MERGE FX PARAMETER ${side} press` },
		{ source: pioneerNote(ch, 0x31), label: `MERGE FX PARAMETER ${side} press + SHIFT` },
		{ source: pioneerNote(ch, 0x70), label: `BEAT FX SELECT (FX${bank}-1)` },
		{ source: pioneerNote(ch, 0x71), label: `BEAT FX SELECT (FX${bank}-2)` },
		{ source: pioneerNote(ch, 0x72), label: `BEAT FX SELECT (FX${bank}-3)` },
		{ source: pioneerNote(ch, 0x1c), label: `BEAT FX CH SELECT CH1 (FX${bank})` },
		{ source: pioneerNote(ch, 0x1d), label: `BEAT FX CH SELECT CH2 (FX${bank})` },
		{ source: pioneerNote(ch, 0x1e), label: `BEAT FX CH SELECT CH3 (FX${bank})` },
		{ source: pioneerNote(ch, 0x1f), label: `BEAT FX CH SELECT CH4 (FX${bank})` },
		{ source: pioneerNote(ch, 0x14), label: `BEAT FX CH SELECT MASTER (FX${bank})` },
		{ source: pioneerNote(ch, 0x06), label: `BEAT FX beat back (FX${bank})` },
		{ source: pioneerNote(ch, 0x66), label: `BEAT FX beat back + SHIFT (FX${bank})` },
		{ source: pioneerNote(ch, 0x07), label: `BEAT FX beat forward (FX${bank})` },
		{ source: pioneerNote(ch, 0x6b), label: `BEAT FX beat forward + SHIFT (FX${bank})` },
		{ source: pioneerCc(ch, 0x02), label: `BEAT FX LEVEL/DEPTH (FX${bank}-1)` },
		{ source: pioneerCc(ch, 0x04), label: `BEAT FX LEVEL/DEPTH (FX${bank}-2)` },
		{ source: pioneerCc(ch, 0x06), label: `BEAT FX LEVEL/DEPTH (FX${bank}-3)` },
		{ source: pioneerNote(ch, 0x47), label: `BEAT FX ON/OFF (FX${bank}-1)` },
		{ source: pioneerNote(ch, 0x48), label: `BEAT FX ON/OFF (FX${bank}-2)` },
		{ source: pioneerNote(ch, 0x49), label: `BEAT FX ON/OFF (FX${bank}-3)` },
		{ source: pioneerNote(ch, 0x43), label: `BEAT FX ON/OFF + SHIFT, release FX (FX${bank})` }
	];
}

function _globalHints(): ControlHint[] {
	const ch = MIXER_GLOBAL_CH;
	return [
		// [PDF] 4-1 BROWSE press, 4-2 BACK, 4-3 VIEW.
		{ source: pioneerNote(ch, 0x41), label: 'BROWSE press' },
		{ source: pioneerNote(ch, 0x42), label: 'BROWSE press + SHIFT' },
		{ source: pioneerNote(ch, 0x65), label: 'BACK' },
		{ source: pioneerNote(ch, 0x66), label: 'BACK + SHIFT' },
		{ source: pioneerNote(ch, 0x7a), label: 'VIEW' },
		{ source: pioneerNote(ch, 0x67), label: 'VIEW long press' },
		{ source: pioneerNote(ch, 0x68), label: 'VIEW + SHIFT' },
		// [PDF] 4-4 .. 4-7 LOAD + SHIFT.
		...DECKS.map((deck): ControlHint => ({
			source: pioneerNote(ch, LOAD_SHIFT_NOTE[deck]),
			label: `LOAD deck ${deck} + SHIFT`
		})),
		// [PDF] 3-3 MASTER CUE + SHIFT: note 98 (0x62).
		{ source: pioneerNote(ch, 0x62), label: 'MASTER CUE + SHIFT' },
		// [PDF] 1-5 DECK + SHIFT: channel 7 notes 124 / 125.
		{ source: pioneerNote(ch, 0x7c), label: 'DECK L + SHIFT' },
		{ source: pioneerNote(ch, 0x7d), label: 'DECK R + SHIFT' },
		// [PDF] 3-4 BOOTH LEVEL CC 9, 3-14 MIC LEVEL CC 5 (MSB).
		{ source: pioneerCc(ch, 0x09), label: 'BOOTH LEVEL' },
		{ source: pioneerCc(ch, 0x05), label: 'MIC LEVEL' },
		// [PDF] 3-1 INPUT SELECT (CH4): channel 4 note 13 (0x0D), DECK4 = 0x00 / SAMPLER = 0x7F.
		{ source: pioneerNote(4, 0x0d), label: 'INPUT SELECT CH4 (DECK 4 / SAMPLER)' }
	];
}

/** LED rules only where the [PDF] prints a MIDI-OUT column for the row. */
function _ledRules(deck: DeckId): LedRule[] {
	const ch = pioneerDeckChannel(deck);
	return [
		// [PDF] 1-1 PLAY MIDI-OUT 0x0B, 1-2 CUE MIDI-OUT 0x0C, 1-7 IN/-4BEAT MIDI-OUT 0x10
		// (RELOOP/EXIT prints no MIDI-OUT, so loop state lights IN and OUT, like rekordbox).
		...pioneerTransportLedRules(deck, 0x10),
		// [PDF] 1-8 OUT MIDI-OUT 0x11.
		{
			trigger: { kind: 'loop_engaged', deck },
			out: { ch, note: 0x11, velocityOn: 0x7f, velocityOff: 0x00 }
		},
		// [PDF] 1-15 BEAT SYNC MIDI-OUT 0x58.
		{
			trigger: { kind: 'beat_sync_enabled', deck },
			out: { ch, note: 0x58, velocityOn: 0x7f, velocityOff: 0x00 }
		},
		// [PDF] 3-8 CH CUE MIDI-OUT 0x54.
		{
			trigger: { kind: 'channel_cue_enabled', deck },
			out: { ch, note: 0x54, velocityOn: 0x7f, velocityOff: 0x00 }
		},
		// [PDF] 5-5 pad MIDI-OUT: note = pad note, OFF 0x00 / ON 0x7F.
		...pioneerPadLedRules(deck, FLX6_PAD_ON, FLX6_PAD_OFF)
	];
}

/** [PDF] 3-11 CH LEVEL METER (CH1..4): MIDI-OUT CC 2 on channels 1-4, 0x00..0x7F. */
export const FLX6_METERS: readonly MidiMeterOutput[] = DECKS.map((deck) => ({
	deck,
	out: { ch: pioneerDeckChannel(deck), cc: 0x02, maxValue: 0x7f }
}));

export const FLX6_MAP: DeviceMap = {
	vendor: 'Pioneer DJ',
	// WebMIDI port names carry the product name, e.g. "DDJ-FLX6" or "DDJ-FLX6-GT".
	nameMatch: 'DDJ-FLX6',
	bindings: [
		...DECKS.flatMap((deck) => _deckBindings(deck)),
		...DECKS.flatMap((deck) => _mixerBindings(deck)),
		...DECKS.flatMap((deck) => _padBindings(deck)),
		..._browserAndGlobalBindings()
	],
	leds: DECKS.flatMap((deck) => _ledRules(deck)),
	meters: [...FLX6_METERS],
	hints: [
		...DECKS.flatMap((deck) => _deckHints(deck)),
		..._fxHints(1),
		..._fxHints(2),
		..._globalHints()
	]
};

/** Self-registration: maps/entries.ts discovers this export, so no shared list names this map. */
export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{ key: 'FLX6_MAP', map: FLX6_MAP, live: true }
];
