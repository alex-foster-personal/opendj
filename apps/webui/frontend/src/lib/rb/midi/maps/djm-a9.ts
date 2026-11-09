/**
 * Pioneer DJ DJM-A9 P0 device map (build unit: DJM-A9 map, epic #9366).
 *
 * SOURCE OF TRUTH for EVERY Note/CC number in this file: the official
 * "DJM-A9 MIDI Message List" PDF (E, version 1.0), from
 * https://downloads.support.alphatheta.com/midi-mapping/dj-mixers/DJM-A9/DJM-A9_MIDI_Message_List_E_10.pdf
 * (not redistributed here; sha256 recorded in
 * docs/controller/research/DJM-A9-MIDI-Message-List.md). Rows are cited as
 * [PDF] with the sheet's own Part No. ids (L1..L22 left column, C1..C10
 * channel strips and crossfader, R1..R29 right column).
 *
 * A MIXER, NOT A DECK: the DJM-A9 has no transport, jog, pads or browser.
 * What the P0 contract can bind is the four channel strips (TRIM, HI, MID,
 * LOW, COLOR as the channel filter, CH FADER, CUE A as channel cue), the
 * CROSSFADER, MASTER LEVEL, HEADPHONES A MIX and LEVEL, and MASTER CUE A.
 * Everything else the sheet documents (microphone section, Sound Color FX,
 * BOOTH, HEADPHONES B, curve switches, MULTI I/O, BEAT FX, input selectors)
 * is a hint, so the practice replica draws it inert with the PARITY-TODO
 * title and the learn log names it.
 *
 * MIDI CHANNEL: the sheet writes every status as 9n / Bn with "n: value of
 * channel No.", the mixer's UTILITY "MIDI CH" setting. This map binds the
 * factory default, channel 1 (A9_MIDI_CH). A unit set to another channel
 * needs that constant changed; nothing here is per-deck channel arithmetic.
 *
 * MIDI-OUT: the sheet has no MIDI-OUT column and AlphaTheta's support note
 * says the DJM-A9 does not accept MIDI input, so this map has no `leds`.
 *
 * SHEET ANOMALIES, transcribed as the pattern of the other three channels
 * and flagged for hardware adjudication (the maintainer owns a DJM-A9):
 *   - [PDF] C7/C8 for CH2 print CUE B as "Bn 11" and CH FADER as "9n 12
 *     Trigger/Toggle". CH1, CH3 and CH4 print CUE B as a note (0x10, 0x12,
 *     0x13) and CH FADER as a CC (0x11, 0x13, 0x14), and CH1's fader is
 *     already CC 0x11, so CH2 is read as CUE B note 0x11 and CH FADER CC 0x12.
 *   - [PDF] R18 X-PAD touch and R29 BEAT FX ON/OFF both print "Bn 72". One
 *     hint carries CC 0x72 and names both.
 *
 * OUT OF CONTRACT (hints only): MIC EQ, TALKOVER, MIC FX, MIC REVERB, MIC ON,
 * BLUETOOTH, Sound Color FX buttons and PARAMETER, LINK CUE and MONO/SPLIT
 * (A and B), CUE B per channel, CROSSFADER ASSIGN, INPUT SELECTOR positions,
 * BOOTH EQ and LEVEL, MASTER CUE B, HEADPHONES B MIX and LEVEL, EQ CURVE,
 * CH FADER CURVE, CROSSFADER CURVE, SEND CH, INSERT SOURCE, SEND LEVEL,
 * INSERT, X-PAD, BEAT, AUTO/TAP, TAP, QUANTIZE, EFFECT FREQUENCY, EFFECT
 * SELECT, CH SELECT, TIME (MSB and LSB), LEVEL/DEPTH, ON/OFF.
 */

import type { ControlHint, DeviceMap, DeviceMapEntry, MidiBinding } from '$lib/rb/midi/midi-types';
import type { DeckId } from '$lib/rb/deck-slots';
import { pioneerCc, pioneerNote } from './pioneer-deck-bindings';

/** [PDF] *1 "n: value of channel No.": the mixer's UTILITY MIDI CH, factory default 1. */
export const A9_MIDI_CH = 1;

const CHANNELS = [1, 2, 3, 4] as const;
type Channel = (typeof CHANNELS)[number];

/** [PDF] C2..C6 and C8 per channel, in sheet order CH1, CH2, CH3, CH4. */
const TRIM_CC: Record<Channel, number> = { 1: 0x01, 2: 0x06, 3: 0x0c, 4: 0x50 };
const HI_CC: Record<Channel, number> = { 1: 0x02, 2: 0x07, 3: 0x0e, 4: 0x51 };
const MID_CC: Record<Channel, number> = { 1: 0x03, 2: 0x08, 3: 0x0f, 4: 0x5c };
const LOW_CC: Record<Channel, number> = { 1: 0x04, 2: 0x09, 3: 0x15, 4: 0x52 };
const COLOR_CC: Record<Channel, number> = { 1: 0x05, 2: 0x0a, 3: 0x16, 4: 0x53 };
const FADER_CC: Record<Channel, number> = { 1: 0x11, 2: 0x12, 3: 0x13, 4: 0x14 };
/** [PDF] C7 CUE A / CUE B per channel. */
const CUE_A_NOTE: Record<Channel, number> = { 1: 0x0a, 2: 0x0b, 3: 0x0c, 4: 0x0d };
const CUE_B_NOTE: Record<Channel, number> = { 1: 0x10, 2: 0x11, 3: 0x12, 4: 0x13 };
/** [PDF] C9 CROSSFADER ASSIGN per channel (0 = A, 0x40 = THRU, 0x7F = B). */
const XF_ASSIGN_CC: Record<Channel, number> = { 1: 0x41, 2: 0x42, 3: 0x43, 4: 0x44 };
/** [PDF] C1 INPUT SELECTOR positions per channel, one note each, in sheet order. */
const INPUT_POSITIONS = [
	'BLUETOOTH',
	'A',
	'B',
	'DIGITAL',
	'LINE',
	'PHONO',
	'RETURN',
	'USB'
] as const;
const INPUT_NOTES: Record<Channel, readonly number[]> = {
	1: [0x51, 0x35, 0x42, 0x32, 0x33, 0x34, 0x43, 0x4a],
	2: [0x52, 0x39, 0x44, 0x36, 0x37, 0x38, 0x45, 0x4b],
	3: [0x53, 0x3d, 0x46, 0x3a, 0x3b, 0x3c, 0x47, 0x4c],
	4: [0x54, 0x41, 0x48, 0x3e, 0x3f, 0x40, 0x49, 0x4d]
};

function _channelBindings(n: Channel): MidiBinding[] {
	const ch = A9_MIDI_CH;
	const deck: DeckId = n;
	return [
		// [PDF] C2 TRIM.
		{ source: pioneerCc(ch, TRIM_CC[n]), action: { type: 'mixer_channel', deck, target: 'trim' } },
		// [PDF] C3 HI, C4 MID, C5 LOW (EQ/ISO).
		{
			source: pioneerCc(ch, HI_CC[n]),
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'high' }
		},
		{
			source: pioneerCc(ch, MID_CC[n]),
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'mid' }
		},
		{
			source: pioneerCc(ch, LOW_CC[n]),
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'low' }
		},
		// [PDF] C6 COLOR: the per-channel Sound Color FX knob, the mixer's filter position.
		{
			source: pioneerCc(ch, COLOR_CC[n]),
			action: { type: 'mixer_channel', deck, target: 'filter' }
		},
		// [PDF] C8 CH FADER (CH2: CC 0x12, see the sheet-anomaly note above).
		{
			source: pioneerCc(ch, FADER_CC[n]),
			action: { type: 'mixer_channel', deck, target: 'fader' }
		},
		// [PDF] C7 CUE A: headphones A cue for this channel.
		{ source: pioneerNote(ch, CUE_A_NOTE[n]), action: { type: 'channel_cue', deck } }
	];
}

function _globalBindings(): MidiBinding[] {
	const ch = A9_MIDI_CH;
	return [
		// [PDF] C10 CROSSFADER: CC 11 (0x0B).
		{ source: pioneerCc(ch, 0x0b), action: { type: 'mixer_global', target: 'crossfader' } },
		// [PDF] R1 MASTER LEVEL: CC 24 (0x18).
		{ source: pioneerCc(ch, 0x18), action: { type: 'mixer_global', target: 'master' } },
		// [PDF] L21 HEADPHONES A MIXING: CC 27 (0x1B). [PDF] L22 LEVEL: CC 26 (0x1A).
		{ source: pioneerCc(ch, 0x1b), action: { type: 'headphone_mix' } },
		{ source: pioneerCc(ch, 0x1a), action: { type: 'headphone_level' } },
		// [PDF] R5 MASTER CUE A: note 14 (0x0E), Trigger/Toggle.
		{ source: pioneerNote(ch, 0x0e), action: { type: 'master_cue', mode: 'latch' } }
	];
}

function _channelHints(n: Channel): ControlHint[] {
	const ch = A9_MIDI_CH;
	return [
		// [PDF] C1 INPUT SELECTOR: one note per position ("previously selected OFF, newly selected ON").
		...INPUT_POSITIONS.map((pos, i): ControlHint => ({
			source: pioneerNote(ch, INPUT_NOTES[n][i]),
			label: `INPUT SELECTOR ${pos} (channel ${n})`
		})),
		// [PDF] C7 CUE B: headphones B cue (CH2: note 0x11, see the sheet-anomaly note above).
		{ source: pioneerNote(ch, CUE_B_NOTE[n]), label: `CUE B (channel ${n})` },
		// [PDF] C9 CROSSFADER ASSIGN switch.
		{ source: pioneerCc(ch, XF_ASSIGN_CC[n]), label: `CROSSFADER ASSIGN A/THRU/B (channel ${n})` }
	];
}

function _leftHints(): ControlHint[] {
	const ch = A9_MIDI_CH;
	return [
		// [PDF] L1..L10 MIC.
		{ source: pioneerCc(ch, 0x1e), label: 'MIC EQ HI' },
		{ source: pioneerCc(ch, 0x1f), label: 'MIC EQ LOW' },
		{ source: pioneerNote(ch, 0x61), label: 'TALK OVER' },
		{ source: pioneerCc(ch, 0x61), label: 'MIC FX ECHO' },
		{ source: pioneerCc(ch, 0x62), label: 'MIC FX PITCH' },
		{ source: pioneerCc(ch, 0x63), label: 'MIC FX MEGAPHONE' },
		{ source: pioneerCc(ch, 0x64), label: 'MIC FX PARAMETER' },
		{ source: pioneerNote(ch, 0x60), label: 'MIC REVERB' },
		{ source: pioneerCc(ch, 0x65), label: 'MIC REVERB PARAMETER' },
		{ source: pioneerNote(ch, 0x62), label: 'MIC ON' },
		// [PDF] L11 BLUETOOTH.
		{ source: pioneerNote(ch, 0x70), label: 'BLUETOOTH' },
		// [PDF] L12..L18 SOUND COLOR FX.
		{ source: pioneerNote(ch, 0x55), label: 'SOUND COLOR FX SPACE' },
		{ source: pioneerNote(ch, 0x69), label: 'SOUND COLOR FX DUB ECHO' },
		{ source: pioneerNote(ch, 0x6a), label: 'SOUND COLOR FX CRUSH' },
		{ source: pioneerNote(ch, 0x6b), label: 'SOUND COLOR FX SWEEP' },
		{ source: pioneerNote(ch, 0x56), label: 'SOUND COLOR FX NOISE' },
		{ source: pioneerNote(ch, 0x57), label: 'SOUND COLOR FX FILTER' },
		{ source: pioneerCc(ch, 0x6c), label: 'SOUND COLOR FX PARAMETER' },
		// [PDF] L19, L20 HEADPHONES A buttons (MIXING and LEVEL are bound).
		{ source: pioneerNote(ch, 0x0f), label: 'LINK CUE A' },
		{ source: pioneerNote(ch, 0x21), label: 'MONO/SPLIT A' }
	];
}

/** [PDF] R25 EFFECT SELECT: one CC per effect, in sheet order. */
const EFFECT_SELECT: readonly (readonly [string, number])[] = [
	['DELAY', 0x2a],
	['ECHO', 0x37],
	['PING PONG', 0x33],
	['SPIRAL', 0x2b],
	['HELIX', 0x3e],
	['REVERB', 0x36],
	['FLANGER', 0x32],
	['PHASER', 0x39],
	['FILTER', 0x3b],
	['TRIPLET FILTER', 0x3a],
	['TRANS', 0x35],
	['ROLL', 0x2e],
	['TRIPLET ROLL', 0x2f],
	['MOBIUS', 0x3d]
];

/** [PDF] R14 SEND CH positions, notes 0x22..0x29, and R26 CH SELECT buttons, notes 1..8. */
const SEND_CH_POSITIONS = ['CH1', 'CH2', 'CH3', 'CH4', 'MIC', 'CF.A', 'CF.B', 'MASTER'] as const;
const CH_SELECT_BUTTONS = ['1', '2', '3', '4', 'MIC', 'CF.A', 'CF.B', 'MST'] as const;

function _rightHints(): ControlHint[] {
	const ch = A9_MIDI_CH;
	return [
		// [PDF] R2..R4 BOOTH MONITOR.
		{ source: pioneerCc(ch, 0x6d), label: 'BOOTH EQ HI' },
		{ source: pioneerCc(ch, 0x6e), label: 'BOOTH EQ LOW' },
		{ source: pioneerCc(ch, 0x19), label: 'BOOTH LEVEL' },
		// [PDF] R6 MASTER CUE B.
		{ source: pioneerNote(ch, 0x14), label: 'MASTER CUE B' },
		// [PDF] R7..R10 HEADPHONES B.
		{ source: pioneerNote(ch, 0x15), label: 'LINK CUE B' },
		{ source: pioneerCc(ch, 0x22), label: 'MONO/SPLIT B' },
		{ source: pioneerCc(ch, 0x55), label: 'HEADPHONES B MIXING' },
		{ source: pioneerCc(ch, 0x56), label: 'HEADPHONES B LEVEL' },
		// [PDF] R11..R13 curve switches.
		{ source: pioneerCc(ch, 0x21), label: 'EQ CURVE ISOLATOR/EQ' },
		{ source: pioneerCc(ch, 0x5e), label: 'CH FADER CURVE' },
		{ source: pioneerCc(ch, 0x5f), label: 'CROSSFADER CURVE' },
		// [PDF] R14..R17 MULTI I/O.
		...SEND_CH_POSITIONS.map((pos, i): ControlHint => ({
			source: pioneerNote(ch, 0x22 + i),
			label: `SEND CH ${pos}`
		})),
		{ source: pioneerNote(ch, 0x6f), label: 'INSERT SOURCE RETURN/USB' },
		{ source: pioneerCc(ch, 0x71), label: 'SEND LEVEL' },
		{ source: pioneerCc(ch, 0x40), label: 'INSERT' },
		// [PDF] R18..R29 BEAT EFFECTS.
		{ source: pioneerCc(ch, 0x74), label: 'X-PAD slide' },
		{
			source: pioneerCc(ch, 0x72),
			label: 'BEAT FX ON/OFF (the sheet prints X-PAD touch on the same CC)'
		},
		{ source: pioneerCc(ch, 0x4c), label: 'BEAT left' },
		{ source: pioneerCc(ch, 0x4d), label: 'BEAT right' },
		{ source: pioneerCc(ch, 0x45), label: 'AUTO/TAP' },
		{ source: pioneerCc(ch, 0x4e), label: 'TAP' },
		{ source: pioneerNote(ch, 0x76), label: 'QUANTIZE' },
		{ source: pioneerCc(ch, 0x68), label: 'EFFECT FREQUENCY LOW' },
		{ source: pioneerCc(ch, 0x67), label: 'EFFECT FREQUENCY MID' },
		{ source: pioneerCc(ch, 0x66), label: 'EFFECT FREQUENCY HI' },
		...EFFECT_SELECT.map(([name, cc]): ControlHint => ({
			source: pioneerCc(ch, cc),
			label: `EFFECT SELECT ${name}`
		})),
		...CH_SELECT_BUTTONS.map((name, i): ControlHint => ({
			source: pioneerNote(ch, 0x01 + i),
			label: `CH SELECT ${name}`
		})),
		{ source: pioneerCc(ch, 0x0d), label: 'TIME (MSB)' },
		{ source: pioneerCc(ch, 0x2d), label: 'TIME (LSB)' },
		{ source: pioneerCc(ch, 0x5b), label: 'LEVEL/DEPTH' }
	];
}

export const DJM_A9_MAP: DeviceMap = {
	vendor: 'Pioneer DJ',
	// WebMIDI port names carry the product name, e.g. "DJM-A9", "Pioneer DJ DJM-A9 MIDI 1".
	nameMatch: 'DJM-A9',
	bindings: [...CHANNELS.flatMap((n) => _channelBindings(n)), ..._globalBindings()],
	hints: [..._leftHints(), ...CHANNELS.flatMap((n) => _channelHints(n)), ..._rightHints()]
};

/** Self-registration: maps/entries.ts discovers this export, so no shared list names this map. */
export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{ key: 'DJM_A9_MAP', map: DJM_A9_MAP, live: true }
];
