/**
 * Pioneer DJ XDJ-RX3 and AlphaTheta XDJ-AZ device maps.
 *
 * SOURCE OF TRUTH for every number: the official MIDI message lists (not
 * redistributed; sha256 in docs/controller/research/XDJ-MIDI-Message-Lists.md).
 * Mixxx ships no mapping for either unit. The unmerged XDJ-RX mapping
 * (mixxxdj/mixxx#3008) sets the BEHAVIOUR reference only: its jog and tempo
 * wires do not exist on these units.
 *
 * HARDWARE: the XDJ-AZ was connected to the set build (da92dbd1) on Fri 6 Nov
 * 2026 (issue #10288): the port name matched this map, PLAY, CUE, pads, browse
 * and LOAD worked, and the jogs worked without SHIFT. Shifted jog twins
 * (CC 41 platter, CC 38 wheel side) are bound here so the jogs keep working
 * while SHIFT is held (issue #10284). The RX3 has not been connected to this code.
 *
 * Channel layout: deck n -> channel n; browser -> 1; mixer and effects -> 5.
 *   RX3 pads: deck n -> channel 5 + n.
 *   AZ pads:  pads 1-4 -> channel 5 + n, pads 5-8 -> channel 9 + n.
 *
 * CH CUE and MASTER CUE are CCs on both units, hence ccButton.
 * The RX3 list gives SHIFT no wire: it is hardware-encoded.
 * The AZ list prints its CH FADER rows as "Upper Side=0", against the RX3 and
 * every other Pioneer list. Read here as lower = 0, and the AZ confirms it: on
 * Fri 6 Nov 2026 the learn log on ch 5 CC 17, 18 and 20 rose (06, 08, 09, 0B,
 * 0D) as the faders were pushed up, so the vendor list is the error.
 *
 * Jog rotation centres on 64 on both units, hence relativeCentered. Paused it
 * scrubs, playing it bends the tempo. On the AZ, SHIFT sends twin CCs 41/38;
 * those bind as deck_jog with scale 4 so a paused scrub is 4x coarser (rekordbox
 * fast-search mimic). Bend while playing uses the same strength as the base
 * wires. There is no scratch: jog touch is unbound.
 *
 * OUT OF CONTRACT (hints only): jog touch, needle search, track and fast search,
 * slip, reverse, quantize, Beat FX. No MidiAction exists for them.
 */

import type {
	ControlHint,
	DeviceMap,
	DeviceMapEntry,
	LedRule,
	MidiBinding
} from '$lib/rb/midi/midi-types';
import type { DeckId } from '$lib/rb/deck-slots';
import type { EqBand } from '$lib/rb/mixer-types';
import type { HotCueSlot } from '$lib/rb/hot-cue-types';
import { pioneerCc, pioneerDeckChannel, pioneerNote } from './pioneer-deck-bindings';

const DECKS = [1, 2, 3, 4] as const;
const MIXER_CH = 5;
const BROWSER_CH = 1;
const HOT_CUE_SLOTS: readonly HotCueSlot[] = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];
const BEAT_LOOP_PAD_BEATS: readonly number[] = [0.25, 0.5, 1, 2, 4, 8, 16, 32];
const PAD_ON = 0x7f;
const PAD_OFF = 0x00;

const TRIM_CC: Record<DeckId, number> = { 1: 1, 2: 6, 3: 12, 4: 80 };
const EQ_CC: Record<EqBand, Record<DeckId, number>> = {
	high: { 1: 2, 2: 7, 3: 14, 4: 81 },
	mid: { 1: 3, 2: 8, 3: 15, 4: 92 },
	low: { 1: 4, 2: 9, 3: 21, 4: 82 }
};
const COLOR_CC: Record<DeckId, number> = { 1: 5, 2: 10, 3: 22, 4: 83 };
const FADER_CC: Record<DeckId, number> = { 1: 17, 2: 18, 3: 19, 4: 20 };
const CH_CUE_CC: Record<DeckId, number> = { 1: 70, 2: 71, 3: 72, 4: 73 };

interface PadWire {
	ch: number;
	hotCueNote: number;
	beatLoopNote: number;
}

type PadWires = (deck: DeckId, pad: number) => PadWire;

const rx3PadWire: PadWires = (deck, pad) => ({
	ch: 5 + deck,
	hotCueNote: pad,
	beatLoopNote: 16 + pad
});

/** AZ pads repeat every 4 with a stride of 19 notes. */
const azPadWire: PadWires = (deck, pad) => {
	const inRow = pad % 4;
	return {
		ch: (pad < 4 ? 5 : 9) + deck,
		hotCueNote: 19 * inRow,
		beatLoopNote: 2 + 19 * inRow
	};
};

function _deckBindings(deck: DeckId): MidiBinding[] {
	const ch = pioneerDeckChannel(deck);
	return [
		{ source: pioneerNote(ch, 0), action: { type: 'deck_play_toggle', deck } },
		{ source: pioneerNote(ch, 1), action: { type: 'deck_cue', deck } },
		{ source: pioneerCc(ch, 0), action: { type: 'deck_pitch', deck, lsbOffset: 32 } },
		{ source: pioneerNote(ch, 31), action: { type: 'deck_sync_toggle', deck } },
		{ source: pioneerNote(ch, 6), action: { type: 'deck_loop_in', deck } },
		{ source: pioneerNote(ch, 7), action: { type: 'deck_loop_out', deck } },
		{ source: pioneerNote(ch, 8), action: { type: 'deck_loop_exit', deck } },
		{ source: pioneerNote(ch, 81), action: { type: 'browse_load', deck } },
		{
			source: pioneerCc(ch, 34),
			action: { type: 'deck_jog', deck },
			relative: true,
			relativeCentered: true
		},
		{
			source: pioneerCc(ch, 33),
			action: { type: 'deck_jog', deck },
			relative: true,
			relativeCentered: true
		}
	];
}

function _mixerBindings(deck: DeckId): MidiBinding[] {
	const bands: EqBand[] = ['high', 'mid', 'low'];
	return [
		{
			source: pioneerCc(MIXER_CH, TRIM_CC[deck]),
			action: { type: 'mixer_channel', deck, target: 'trim' }
		},
		...bands.map((band): MidiBinding => ({
			source: pioneerCc(MIXER_CH, EQ_CC[band][deck]),
			action: { type: 'mixer_channel', deck, target: 'eq', band }
		})),
		{
			source: pioneerCc(MIXER_CH, COLOR_CC[deck]),
			action: { type: 'mixer_channel', deck, target: 'filter' }
		},
		{
			source: pioneerCc(MIXER_CH, FADER_CC[deck]),
			action: { type: 'mixer_channel', deck, target: 'fader' }
		},
		{
			source: pioneerCc(MIXER_CH, CH_CUE_CC[deck]),
			action: { type: 'channel_cue', deck },
			ccButton: true
		}
	];
}

function _padBindings(deck: DeckId, padWire: PadWires): MidiBinding[] {
	return HOT_CUE_SLOTS.flatMap((slot, pad): MidiBinding[] => {
		const wire = padWire(deck, pad);
		return [
			{
				source: pioneerNote(wire.ch, wire.hotCueNote),
				action: { type: 'deck_hot_cue', deck, slot }
			},
			{
				source: pioneerNote(wire.ch, wire.beatLoopNote),
				action: { type: 'deck_beat_loop', deck, beats: BEAT_LOOP_PAD_BEATS[pad] }
			}
		];
	});
}

function _globalBindings(): MidiBinding[] {
	return [
		{ source: pioneerCc(MIXER_CH, 11), action: { type: 'mixer_global', target: 'crossfader' } },
		{ source: pioneerCc(MIXER_CH, 24), action: { type: 'mixer_global', target: 'master' } },
		{ source: pioneerCc(MIXER_CH, 27), action: { type: 'headphone_mix' } },
		{ source: pioneerCc(MIXER_CH, 26), action: { type: 'headphone_level' } },
		{
			source: pioneerCc(MIXER_CH, 74),
			action: { type: 'master_cue', mode: 'latch' },
			ccButton: true
		},
		{
			source: pioneerCc(BROWSER_CH, 79),
			action: { type: 'browse_encoder' },
			relative: true
		}
	];
}

function _ledRules(deck: DeckId, padWire: PadWires): LedRule[] {
	const ch = pioneerDeckChannel(deck);
	return [
		{
			trigger: { kind: 'deck_playing', deck },
			out: { ch, note: 0, velocityOn: 0x7f, velocityOff: 0x00 }
		},
		{
			trigger: { kind: 'beat_sync_enabled', deck },
			out: { ch, note: 31, velocityOn: 0x7f, velocityOff: 0x00 }
		},
		{
			trigger: { kind: 'loop_engaged', deck },
			out: { ch, note: 8, velocityOn: 0x7f, velocityOff: 0x00 }
		},
		...HOT_CUE_SLOTS.map((slot, pad): LedRule => {
			const wire = padWire(deck, pad);
			return {
				trigger: { kind: 'hot_cue_present', deck, slot },
				out: { ch: wire.ch, note: wire.hotCueNote, velocityOn: PAD_ON, velocityOff: PAD_OFF }
			};
		})
	];
}

function _deckHints(deck: DeckId): ControlHint[] {
	const ch = pioneerDeckChannel(deck);
	const label = (name: string): string => `${name} (deck ${deck})`;
	return [
		{ source: pioneerNote(ch, 32), label: label('JOG touch') },
		{ source: pioneerCc(ch, 28), label: label('NEEDLE POSITION') },
		{ source: pioneerNote(ch, 2), label: label('SEARCH FWD') },
		{ source: pioneerNote(ch, 3), label: label('SEARCH REV') },
		{ source: pioneerNote(ch, 4), label: label('TRACK SEARCH FWD') },
		{ source: pioneerNote(ch, 5), label: label('TRACK SEARCH REV') },
		{ source: pioneerNote(ch, 44), label: label('SLIP') },
		{ source: pioneerNote(ch, 33), label: label('REVERSE') },
		{ source: pioneerNote(ch, 17), label: label('MASTER TEMPO') }
	];
}

interface XdjExtras {
	bindings: MidiBinding[];
	leds: LedRule[];
	hints: ControlHint[];
}

function _xdjMap(nameMatch: string, padWire: PadWires, extra: XdjExtras): DeviceMap {
	return {
		vendor: 'Pioneer DJ',
		nameMatch,
		bindings: [
			...DECKS.flatMap((deck) => _deckBindings(deck)),
			...DECKS.flatMap((deck) => _mixerBindings(deck)),
			...DECKS.flatMap((deck) => _padBindings(deck, padWire)),
			..._globalBindings(),
			...extra.bindings
		],
		leds: [...DECKS.flatMap((deck) => _ledRules(deck, padWire)), ...extra.leds],
		hints: [...DECKS.flatMap((deck) => _deckHints(deck)), ...extra.hints]
	};
}

function _azOnlyBindings(deck: DeckId): MidiBinding[] {
	const ch = pioneerDeckChannel(deck);
	return [
		{ source: pioneerNote(ch, 63), action: { type: 'shift_modifier' } },
		{ source: pioneerNote(ch, 20), action: { type: 'deck_key_sync_toggle', deck } },
		{ source: pioneerNote(ch, 67), action: { type: 'deck_beat_loop', deck, beats: 4 } },
		{ source: pioneerNote(ch, 68), action: { type: 'deck_beat_loop', deck, beats: 8 } },
		// SHIFT twins: platter CC 41 (base 34), wheel side CC 38 (base 33).
		{
			source: pioneerCc(ch, 41),
			action: { type: 'deck_jog', deck, scale: 4 },
			relative: true,
			relativeCentered: true
		},
		{
			source: pioneerCc(ch, 38),
			action: { type: 'deck_jog', deck, scale: 4 },
			relative: true,
			relativeCentered: true
		},
		// 23/25 are the hardware SHIFT twins (distinct note ids, not shift: true).
		{ source: pioneerNote(ch, 22), action: { type: 'deck_beat_jump', deck, beats: 1 } },
		{ source: pioneerNote(ch, 23), action: { type: 'deck_beat_jump', deck, beats: 4 } },
		{ source: pioneerNote(ch, 24), action: { type: 'deck_beat_jump', deck, beats: -1 } },
		{ source: pioneerNote(ch, 25), action: { type: 'deck_beat_jump', deck, beats: -4 } }
	];
}

/** AZ MIDI-OUT rows ("same as MIDI IN") with an engine trigger: CUE, 4 BEAT, 8 BEAT. */
function _azOnlyLeds(deck: DeckId): LedRule[] {
	const ch = pioneerDeckChannel(deck);
	return [
		{
			trigger: { kind: 'deck_loaded', deck },
			out: { ch, note: 1, velocityOn: 0x7f, velocityOff: 0x00 }
		},
		{
			trigger: { kind: 'loop_beats_engaged', deck, beats: 4 },
			out: { ch, note: 67, velocityOn: 0x7f, velocityOff: 0x00 }
		},
		{
			trigger: { kind: 'loop_beats_engaged', deck, beats: 8 },
			out: { ch, note: 68, velocityOn: 0x7f, velocityOff: 0x00 }
		}
	];
}

/** Documented AZ deck rows the practice replica draws but no MidiAction covers.
 * Every number is a row of the AZ list (group 1 DECK, group 5 PERFORMANCE PADS). */
function _azDeckHints(deck: DeckId): ControlHint[] {
	const ch = pioneerDeckChannel(deck);
	const label = (name: string): string => `${name} (deck ${deck})`;
	const rows: [number, string][] = [
		[19, 'TEMPO RESET'],
		[16, 'TEMPO RANGE'],
		[21, 'DECK SELECT'],
		[18, 'JOG MODE'],
		[42, 'VINYL SPEED ADJUST'],
		[11, 'CUE/LOOP CALL NEXT'],
		[12, 'CUE/LOOP CALL PREV'],
		[13, 'CUE/LOOP DELETE'],
		[10, 'CUE/LOOP MEMORY'],
		[26, 'DECK QUANTIZE'],
		[30, 'SYNC MASTER'],
		[34, 'PAD MODE HOT CUE'],
		[35, 'PAD MODE BEAT LOOP'],
		[36, 'PAD MODE SLIP LOOP'],
		[37, 'PAD MODE BEAT JUMP'],
		[38, 'PAD MODE GATE CUE +SHIFT'],
		[39, 'PAD MODE BEAT LOOP2 +SHIFT'],
		[40, 'PAD MODE KEY SHIFT +SHIFT'],
		[41, 'PAD MODE BEAT JUMP2 +SHIFT']
	];
	return [
		...rows.map(([note, name]): ControlHint => ({
			source: pioneerNote(ch, note),
			label: label(name)
		})),
		...HOT_CUE_SLOTS.flatMap((_, pad): ControlHint[] => {
			const wire = azPadWire(deck, pad);
			// All remaining documented rows are inspection hints, never invented actions.
			const modes: [number, string][] = [
				[1, 'HOT CUE +SHIFT'],
				[3, 'BEAT LOOP +SHIFT'],
				[4, 'PAD FX1 PAGE1'],
				[5, 'PAD FX1 PAGE2'],
				[6, 'PAD FX2 PAGE1'],
				[7, 'PAD FX2 PAGE2'],
				[8, 'SLIP LOOP +SHIFT'],
				[9, 'BEAT JUMP'],
				[10, 'BEAT JUMP +SHIFT'],
				[11, 'GATE CUE'],
				[12, 'GATE CUE +SHIFT'],
				[13, 'BEAT LOOP2'],
				[14, 'BEAT LOOP2 +SHIFT'],
				[15, 'KEY SHIFT'],
				[16, 'KEY SHIFT +SHIFT'],
				[17, 'BEAT JUMP2'],
				[18, 'BEAT JUMP2 +SHIFT']
			];
			return modes.map(([offset, mode]) => ({
				source: pioneerNote(wire.ch, wire.hotCueNote + offset),
				label: label(`${mode} PAD ${pad + 1}`)
			}));
		})
	];
}

/** Documented AZ effect, mixer and browser rows with no MidiAction (groups 2, 3, 4). */
const _AZ_GLOBAL_HINTS: ControlHint[] = [
	...(
		[
			[116, 'X-PAD slide'],
			[76, 'BEAT LEFT'],
			[77, 'BEAT RIGHT'],
			[69, 'AUTO/TAP'],
			[78, 'TAP'],
			[102, 'FX FREQUENCY HI'],
			[103, 'FX FREQUENCY MID'],
			[104, 'FX FREQUENCY LOW'],
			[42, 'EFFECT SELECT (DELAY position)'],
			[34, 'FX CH SELECT CH3'],
			[35, 'FX CH SELECT CH1'],
			[36, 'FX CH SELECT CH2'],
			[37, 'FX CH SELECT CH4'],
			[28, 'FX CH SELECT MIC'],
			[39, 'FX CH SELECT CF.A'],
			[40, 'FX CH SELECT CF.B'],
			[41, 'FX CH SELECT MASTER'],
			[45, 'FX TIME'],
			[91, 'FX LEVEL/DEPTH'],
			[114, 'BEAT FX ON/OFF'],
			[108, 'SOUND COLOR FX PARAMETER'],
			[105, 'SOUND COLOR FX SPACE'],
			[107, 'SOUND COLOR FX DUB ECHO'],
			[86, 'SOUND COLOR FX CRUSH'],
			[106, 'SOUND COLOR FX SWEEP'],
			[85, 'SOUND COLOR FX NOISE'],
			[87, 'SOUND COLOR FX FILTER'],
			[84, 'LINK CUE'],
			[49, 'MASTER EQ LOW'],
			[48, 'MASTER EQ MID'],
			[47, 'MASTER EQ HI'],
			[25, 'BOOTH MONITOR LEVEL'],
			[75, 'MIC 1 SWITCH'],
			[79, 'MIC 2 SWITCH'],
			[89, 'TALKOVER'],
			[31, 'MIC 1 EQ LOW'],
			[98, 'MIC 1 EQ MID'],
			[30, 'MIC 1 EQ HI'],
			[97, 'MIC 2 EQ LOW'],
			[99, 'MIC 2 EQ MID'],
			[96, 'MIC 2 EQ HI']
		] as [number, string][]
	).map(([cc, label]): ControlHint => ({ source: pioneerCc(MIXER_CH, cc), label })),
	{ source: pioneerNote(MIXER_CH, 118), label: 'FX QUANTIZE' },
	{ source: pioneerCc(BROWSER_CH, 96), label: 'ROTARY SELECTOR +SHIFT' },
	{ source: pioneerNote(BROWSER_CH, 50), label: 'BACK' },
	{ source: pioneerNote(BROWSER_CH, 48), label: 'TAG TRACK/REMOVE' },
	{ source: pioneerNote(BROWSER_CH, 49), label: 'TRACK FILTER' }
];

/**
 * RX3 rows the list documents but no MidiAction covers, so the practice replica can place
 * every top-panel control as a hint (inert, PARITY-TODO) and never a fake binding. Every
 * number is a row of the RX3 list; fig numbers are the list's. The AZ list is a different
 * document and gets none of these.
 */
function _rx3DeckHints(deck: DeckId): ControlHint[] {
	const ch = pioneerDeckChannel(deck);
	const label = (name: string): string => `${name} (deck ${deck})`;
	const notes: [number, string][] = [
		// 1-1 jog touch + SHIFT
		[72, 'JOG touch + SHIFT'],
		// 1-4 TEMPO RANGE, 1-6 MASTER, 1-7 JOG MODE, 1-8-1 VINYL SPEED ADJUST
		[16, 'TEMPO RANGE'],
		[30, 'MASTER'],
		[18, 'JOG MODE'],
		[42, 'VINYL SPEED ADJUST'],
		// 1-5 SYNC long press, 1-14 LOOP IN long press
		[71, 'SYNC long press'],
		[69, 'LOOP IN long press'],
		// 1-17 CUE/LOOP CALL, 1-18 CUE/LOOP DELETE, 1-19 CUE/LOOP MEMORY, 1-25 QUANTIZE
		[11, 'CUE/LOOP CALL NEXT'],
		[12, 'CUE/LOOP CALL PREV'],
		[13, 'CUE/LOOP DELETE'],
		[10, 'CUE/LOOP MEMORY'],
		[9, 'QUANTIZE'],
		// 5-1..5-4 pad mode buttons (deck channel, not the pad channel) and their +SHIFT rows
		[34, 'HOT CUE mode'],
		[38, 'HOT CUE mode + SHIFT'],
		[35, 'BEAT LOOP mode'],
		[39, 'BEAT LOOP mode + SHIFT'],
		[36, 'SLIP LOOP mode'],
		[40, 'SLIP LOOP mode + SHIFT'],
		[37, 'BEAT JUMP mode'],
		[41, 'BEAT JUMP mode + SHIFT']
	];
	return [
		...notes.map(([note, name]): ControlHint => ({
			source: pioneerNote(ch, note),
			label: label(name)
		})),
		// 1-1 jog platter and wheel side + SHIFT, 3-13 INPUT SWITCH (deck channel, CC 99)
		{ source: pioneerCc(ch, 41), label: label('JOG platter + SHIFT') },
		{ source: pioneerCc(ch, 38), label: label('JOG wheel side + SHIFT') },
		{ source: pioneerCc(ch, 99), label: label('INPUT SWITCH') }
	];
}

/** 5-5..5-12 shifted pad rows on the pad channel (HOT CUE mode + SHIFT = GATE CUE). */
function _rx3PadHints(deck: DeckId): ControlHint[] {
	return HOT_CUE_SLOTS.map((_slot, pad): ControlHint => {
		const wire = rx3PadWire(deck, pad);
		return {
			source: pioneerNote(wire.ch, 8 + pad),
			label: `PAD ${pad + 1} + SHIFT (deck ${deck})`
		};
	});
}

/** 2-3 EFFECT SELECT rotary: one CC per detent, in the list's order. */
const RX3_EFFECT_SELECT_CC: readonly (readonly [number, string])[] = [
	[42, 'DELAY'],
	[55, 'ECHO'],
	[51, 'PING PONG'],
	[43, 'SPIRAL'],
	[54, 'REVERB'],
	[53, 'TRANS'],
	[59, 'FILTER'],
	[50, 'FLANGER'],
	[57, 'PHASER'],
	[63, 'PITCH'],
	[58, 'SLIP ROLL'],
	[46, 'ROLL'],
	[61, 'VINYL BRAKE'],
	[62, 'HELIX']
];

/** 2-4 CH SELECT rotary: one CC per detent, in the list's order. */
const RX3_CH_SELECT_CC: readonly (readonly [number, string])[] = [
	[39, 'CF.A'],
	[40, 'CF.B'],
	[28, 'MIC'],
	[32, 'AUX'],
	[34, 'CH1'],
	[35, 'CH2'],
	[41, 'MASTER']
];

/** Documented RX3 effect, mixer and browser rows with no MidiAction (groups 2, 3, 4). */
function _rx3GlobalHints(): ControlHint[] {
	const mixer: [number, string][] = [
		// 2-1 BEAT, 2-2 TAP (+SHIFT), 2-5 TIME, 2-6 LEVEL DEPTH, 2-7 BEAT EFFECT ON/OFF (+SHIFT)
		[76, 'BEAT LEFT'],
		[77, 'BEAT RIGHT'],
		[78, 'TAP'],
		[126, 'TAP + SHIFT'],
		[45, 'BEAT FX TIME'],
		[91, 'BEAT FX LEVEL/DEPTH'],
		[114, 'BEAT FX ON/OFF'],
		[52, 'BEAT FX ON/OFF + SHIFT'],
		// 2-9 PARAMETER, 2-10..2-15 SOUND COLOR FX buttons
		[108, 'SOUND COLOR FX PARAMETER'],
		[105, 'SOUND COLOR FX SPACE'],
		[107, 'SOUND COLOR FX DUB ECHO'],
		[106, 'SOUND COLOR FX SWEEP'],
		[85, 'SOUND COLOR FX NOISE'],
		[86, 'SOUND COLOR FX CRUSH'],
		[87, 'SOUND COLOR FX FILTER'],
		// 3-3 CROSSFADER curve switch, 3-12 LINK CUE, 3-15 BOOTH, 3-17 AUX SELECT, 3-18 AUX TRIM
		[95, 'CROSSFADER CURVE'],
		[75, 'LINK CUE'],
		[25, 'BOOTH MONITOR LEVEL'],
		[119, 'AUX SELECT OFF'],
		[120, 'AUX SELECT LINE'],
		[121, 'AUX SELECT PORTABLE'],
		[122, 'AUX TRIM']
	];
	return [
		...mixer.map(([cc, label]): ControlHint => ({ source: pioneerCc(MIXER_CH, cc), label })),
		...RX3_EFFECT_SELECT_CC.map(([cc, name]): ControlHint => ({
			source: pioneerCc(MIXER_CH, cc),
			label: `EFFECT SELECT ${name}`
		})),
		...RX3_CH_SELECT_CC.map(([cc, name]): ControlHint => ({
			source: pioneerCc(MIXER_CH, cc),
			label: `CH SELECT ${name}`
		})),
		// 4-1 ROTARY SELECTOR +SHIFT and press, 4-2 BACK, 4-3 TAG TRACK/REMOVE, 4-4 TRACK FILTER
		{ source: pioneerCc(BROWSER_CH, 96), label: 'ROTARY SELECTOR + SHIFT' },
		{ source: pioneerNote(BROWSER_CH, 51), label: 'ROTARY SELECTOR press' },
		{ source: pioneerNote(BROWSER_CH, 97), label: 'ROTARY SELECTOR press + SHIFT' },
		{ source: pioneerNote(BROWSER_CH, 50), label: 'BACK' },
		{ source: pioneerNote(BROWSER_CH, 95), label: 'BACK + SHIFT' },
		{ source: pioneerNote(BROWSER_CH, 48), label: 'TAG TRACK/REMOVE' },
		{ source: pioneerNote(BROWSER_CH, 98), label: 'TAG TRACK/REMOVE + SHIFT' },
		{ source: pioneerNote(BROWSER_CH, 49), label: 'TRACK FILTER' },
		{ source: pioneerNote(BROWSER_CH, 93), label: 'TRACK FILTER + SHIFT' }
	];
}

/**
 * RX3 MIDI-OUT rows ("<- same as MIDI IN") an existing LedTrigger can drive: 1-21 CUE
 * lights when a track is loaded, 5-1 HOT CUE mode lights while that pad mode is selected.
 * PLAY, SYNC, RELOOP/EXIT and the hot-cue pads are already in _ledRules. The other OUT
 * rows (MASTER TEMPO, MASTER, JOG MODE, REVERSE, SLIP, LOOP IN/OUT, LOAD, QUANTIZE, the
 * other pad modes) have no trigger kind yet, and the CC-status rows (CH CUE, TAP, BEAT FX
 * ON/OFF, SOUND COLOR FX) cannot be a Note On LedRule; PARITY-TODO.
 */
function _rx3Leds(deck: DeckId): LedRule[] {
	const ch = pioneerDeckChannel(deck);
	return [
		{
			trigger: { kind: 'deck_loaded', deck },
			out: { ch, note: 1, velocityOn: 0x7f, velocityOff: 0x00 }
		},
		{
			trigger: { kind: 'pad_mode_selected', deck, mode: 'hot_cue' },
			out: { ch, note: 34, velocityOn: 0x7f, velocityOff: 0x00 }
		}
	];
}

export const XDJ_RX3_MAP: DeviceMap = _xdjMap('XDJ-RX3', rx3PadWire, {
	bindings: [],
	leds: DECKS.flatMap((deck) => _rx3Leds(deck)),
	hints: [
		...DECKS.flatMap((deck) => _rx3DeckHints(deck)),
		...DECKS.flatMap((deck) => _rx3PadHints(deck)),
		..._rx3GlobalHints()
	]
});
export const XDJ_AZ_MAP: DeviceMap = _xdjMap('XDJ-AZ', azPadWire, {
	bindings: DECKS.flatMap((deck) => _azOnlyBindings(deck)),
	leds: DECKS.flatMap((deck) => _azOnlyLeds(deck)),
	hints: [...DECKS.flatMap((deck) => _azDeckHints(deck)), ..._AZ_GLOBAL_HINTS]
});

/** Self-registration: maps/entries.ts discovers this export, so no shared list names this map. */
export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{ key: 'XDJ_RX3_MAP', map: XDJ_RX3_MAP, live: true },
	{ key: 'XDJ_AZ_MAP', map: XDJ_AZ_MAP, live: true }
];
