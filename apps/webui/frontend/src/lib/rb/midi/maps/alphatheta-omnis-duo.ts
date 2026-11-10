/**
 * AlphaTheta OMNIS-DUO device map (two-deck portable all-in-one; deck replica epic #9366).
 *
 * SOURCE OF TRUTH for EVERY Note/CC number in this file: the official AlphaTheta
 * "OMNIS-DUO MIDI Message List" PDF (E1, 3 pages, file dated Thu 21 Dec 2023), from
 * https://downloads.support.alphatheta.com/software_info/all-in-one-dj-systems/OMNIS-DUO/OMNIS-DUO_MIDI_Message_List_E1.pdf
 * (not redistributed here; sha256 recorded in
 * docs/controller/research/OMNIS-DUO-MIDI-Message-List.md). [PDF] n-m below is the
 * figure number printed on that list's top-panel drawing.
 *
 * NOT HARDWARE CONFIRMED: no OMNIS-DUO has been connected to this code.
 *
 * Channel layout ([PDF] MIDI channel assign):
 *   BROWSE and DECK1           -> channel 1
 *   DECK2                      -> channel 2
 *   (DECK3 / DECK4 -> 3 / 4 exist in the list for PC-control deck select; the unit has
 *    two physical decks, so this map binds decks 1 and 2 only, like the FLX4 map)
 *   EFFECT and MIXER           -> channel 5
 *   HOT CUE pads deck 1 / 2    -> channels 6 / 7
 *   OTHERS (Bluetooth)         -> channel 12
 *
 * Like the XDJ all-in-ones: CH CUE is a CC button (ccButton), the jog centres on 64
 * (relativeCentered) and BROWSE sends two's-complement ticks (relative). SHIFT is a wire
 * (note 63) so midiState.shiftHeld stays truthful; shifted controls send their own numbers.
 *
 * 14-BIT: TEMPO is the only MSB/LSB pair (CC 0 / CC 32, lsbOffset 32).
 *
 * LED feedback: only the rows the list marks "same as MIDI IN" and sends as NOTE are modelled
 * (PLAY, CUE, HOT CUE A-H). CH CUE and BEAT FX ON/OFF feedback are CCs, which LedRule
 * cannot send, so they stay unlit. CH LEVEL METER ([PDF] 3-8, Bn CC 2) is a meter output.
 *
 * OUT OF CONTRACT (hints only, no MidiAction exists): BEAT JUMP, the BEAT LOOP size
 * encoder turn, jog touch, Beat FX, BOOTH, MIC, MIC ON, Bluetooth source transport, hot-cue
 * delete (pad + SHIFT), BROWSE press, crossfader curve, PC-control and mixer-mode messages,
 * and the on-screen rows (JOG MODE, AUTO CUE, QUANTIZE, SLIP, MASTER TEMPO, TEMPO RANGE,
 * CUE/LOOP CALL, MEMORY/DELETE, NEEDLE SEARCH, DECK SELECT). On-screen SYNC is bound because
 * the unit still sends it over MIDI.
 */

import type {
	ControlHint,
	DeviceMap,
	DeviceMapEntry,
	LedRule,
	MidiBinding,
	MidiMeterOutput
} from '$lib/rb/midi/midi-types';
import type { EqBand } from '$lib/rb/mixer-types';
import type { HotCueSlot } from '$lib/rb/hot-cue-types';
import { pioneerCc, pioneerDeckChannel, pioneerNote } from './pioneer-deck-bindings';

const DECKS = [1, 2] as const;
type OmnisDeck = (typeof DECKS)[number];

const MIXER_CH = 5;
const BROWSER_CH = 1;
const BLUETOOTH_CH = 12;
const HOT_CUE_SLOTS: readonly HotCueSlot[] = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];
export const OMNIS_DUO_PAD_ON = 0x7f;
export const OMNIS_DUO_PAD_OFF = 0x00;

/** [PDF] MIDI channel assign: HOT CUE deck 1 -> 6, deck 2 -> 7. */
export function omnisDuoPadChannel(deck: OmnisDeck): number {
	return 5 + deck;
}

// [PDF] 3-1 TRIM, 3-2 EQ, 2-4 COLOR, 3-7 CH FADER, 3-3 CH CUE: all on channel 5.
const TRIM_CC: Record<OmnisDeck, number> = { 1: 1, 2: 6 };
const EQ_CC: Record<EqBand, Record<OmnisDeck, number>> = {
	high: { 1: 2, 2: 7 },
	mid: { 1: 3, 2: 8 },
	low: { 1: 4, 2: 9 }
};
const COLOR_CC: Record<OmnisDeck, number> = { 1: 5, 2: 10 };
const FADER_CC: Record<OmnisDeck, number> = { 1: 17, 2: 18 };
const CH_CUE_CC: Record<OmnisDeck, number> = { 1: 70, 2: 71 };

function _deckBindings(deck: OmnisDeck): MidiBinding[] {
	const ch = pioneerDeckChannel(deck);
	return [
		// [PDF] 1-7 PLAY/PAUSE: note 0.
		{ source: pioneerNote(ch, 0), action: { type: 'deck_play_toggle', deck } },
		// [PDF] 1-6 CUE: note 1.
		{ source: pioneerNote(ch, 1), action: { type: 'deck_cue', deck } },
		// [PDF] 1-5 SHIFT: note 63.
		{ source: pioneerNote(ch, 63), action: { type: 'shift_modifier' } },
		// [PDF] 1-16 TEMPO: CC 0 MSB / CC 32 LSB, 0..16383.
		{ source: pioneerCc(ch, 0), action: { type: 'deck_pitch', deck, lsbOffset: 32 } },
		// [PDF] 1-17 LOAD: note 81 on the deck's own channel.
		{ source: pioneerNote(ch, 81), action: { type: 'browse_load', deck } },
		// [PDF] 1-3 BEAT LOOP press: note 67 (loop of the selected length on/off);
		// + SHIFT (RELOOP): note 68, read as loop exit.
		{ source: pioneerNote(ch, 67), action: { type: 'deck_auto_loop_toggle', deck } },
		{ source: pioneerNote(ch, 68), action: { type: 'deck_loop_exit', deck } },
		// [PDF] 1-23 SYNC (on-screen button, still sent over MIDI): note 31.
		{ source: pioneerNote(ch, 31), action: { type: 'deck_sync_toggle', deck } },
		// [PDF] 1-4 Jog wheel top CC 34 / side CC 33; clockwise increases from 65.
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

function _mixerBindings(deck: OmnisDeck): MidiBinding[] {
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

/** [PDF] 1-8..1-15 HOT CUE A-H: notes 0-7 on the deck's pad channel. */
function _padBindings(deck: OmnisDeck): MidiBinding[] {
	const ch = omnisDuoPadChannel(deck);
	return HOT_CUE_SLOTS.map((slot, pad): MidiBinding => ({
		source: pioneerNote(ch, pad),
		action: { type: 'deck_hot_cue', deck, slot }
	}));
}

function _globalBindings(): MidiBinding[] {
	return [
		// [PDF] 3-6 CROSSFADER: CC 11. 3-10 MASTER LEVEL: CC 24.
		{ source: pioneerCc(MIXER_CH, 11), action: { type: 'mixer_global', target: 'crossfader' } },
		{ source: pioneerCc(MIXER_CH, 24), action: { type: 'mixer_global', target: 'master' } },
		// [PDF] 3-4 HEADPHONES CUE/MASTER: CC 27. 3-5 HEADPHONES LEVEL: CC 26.
		{ source: pioneerCc(MIXER_CH, 27), action: { type: 'headphone_mix' } },
		{ source: pioneerCc(MIXER_CH, 26), action: { type: 'headphone_level' } },
		// [PDF] 4-1 ROTARY SELECTOR turn: CC 79, + SHIFT CC 96; CW 1..30, CCW 127..98.
		{ source: pioneerCc(BROWSER_CH, 79), action: { type: 'browse_encoder' }, relative: true },
		{ source: pioneerCc(BROWSER_CH, 96), action: { type: 'browse_encoder' }, relative: true }
	];
}

function _ledRules(deck: OmnisDeck): LedRule[] {
	const ch = pioneerDeckChannel(deck);
	const padCh = omnisDuoPadChannel(deck);
	return [
		// [PDF] 1-7 PLAY/PAUSE MIDI-OUT note 0; 1-6 CUE MIDI-OUT note 1.
		{
			trigger: { kind: 'deck_playing', deck },
			out: { ch, note: 0, velocityOn: 0x7f, velocityOff: 0x00 }
		},
		{
			trigger: { kind: 'deck_loaded', deck },
			out: { ch, note: 1, velocityOn: 0x7f, velocityOff: 0x00 }
		},
		// [PDF] 1-8..1-15 HOT CUE MIDI-OUT notes 0-7 on the pad channel.
		...HOT_CUE_SLOTS.map((slot, pad): LedRule => ({
			trigger: { kind: 'hot_cue_present', deck, slot },
			out: { ch: padCh, note: pad, velocityOn: OMNIS_DUO_PAD_ON, velocityOff: OMNIS_DUO_PAD_OFF }
		}))
	];
}

/** [PDF] 3-8 CH LEVEL METER: MIDI-OUT Bn CC 2 on the deck channel (65..127 lights the bar). */
function _meters(): MidiMeterOutput[] {
	return DECKS.map((deck): MidiMeterOutput => ({
		deck,
		out: { ch: pioneerDeckChannel(deck), cc: 2, maxValue: 127 }
	}));
}

function _deckHints(deck: OmnisDeck): ControlHint[] {
	const ch = pioneerDeckChannel(deck);
	const label = (name: string): string => `${name} (deck ${deck})`;
	return [
		// [PDF] 1-1 / 1-2 BEAT JUMP REV / FWD, with + SHIFT rows.
		{ source: pioneerNote(ch, 45), label: label('BEAT JUMP REV') },
		{ source: pioneerNote(ch, 74), label: label('BEAT JUMP REV + SHIFT') },
		{ source: pioneerNote(ch, 44), label: label('BEAT JUMP FWD') },
		{ source: pioneerNote(ch, 73), label: label('BEAT JUMP FWD + SHIFT') },
		// [PDF] 1-3 BEAT LOOP turn (loop length select).
		{ source: pioneerCc(ch, 46), label: label('BEAT LOOP size') },
		// [PDF] 1-4 Jog: + SHIFT rotation rows and touch.
		{ source: pioneerCc(ch, 41), label: label('JOG top + SHIFT') },
		{ source: pioneerCc(ch, 38), label: label('JOG side + SHIFT') },
		{ source: pioneerNote(ch, 32), label: label('JOG touch') },
		{ source: pioneerNote(ch, 72), label: label('JOG touch + SHIFT') },
		// [PDF] 1-6 CUE + SHIFT.
		{ source: pioneerNote(ch, 5), label: label('CUE + SHIFT') },
		// [PDF] 1-18..1-22, 1-24..1-26: on-screen deck rows.
		{ source: pioneerNote(ch, 18), label: label('JOG MODE') },
		{ source: pioneerNote(ch, 15), label: label('AUTO CUE') },
		{ source: pioneerNote(ch, 9), label: label('QUANTIZE') },
		{ source: pioneerNote(ch, 43), label: label('SLIP') },
		{ source: pioneerNote(ch, 17), label: label('MASTER TEMPO') },
		{ source: pioneerNote(ch, 20), label: label('TEMPO RANGE +/-6%') },
		{ source: pioneerNote(ch, 21), label: label('TEMPO RANGE +/-10%') },
		{ source: pioneerNote(ch, 22), label: label('TEMPO RANGE +/-16%') },
		{ source: pioneerNote(ch, 23), label: label('TEMPO RANGE WIDE') },
		{ source: pioneerNote(ch, 11), label: label('CUE/LOOP CALL NEXT') },
		{ source: pioneerNote(ch, 12), label: label('CUE/LOOP CALL PREV') },
		{ source: pioneerNote(ch, 10), label: label('MEMORY/DELETE') },
		{ source: pioneerCc(ch, 28), label: label('NEEDLE POSITION') },
		{ source: pioneerNote(ch, 54), label: label('NEEDLE POSITION touch') },
		// [PDF] 4-2 DECK SELECT (CC 102) and 5 PC Control mode (CC 100).
		{ source: pioneerCc(ch, 102), label: label('DECK SELECT') },
		{ source: pioneerCc(ch, 100), label: label('PC CONTROL mode') }
	];
}

/** [PDF] 1-8..1-15 + SHIFT: notes 8-15 on the pad channel (hot cue delete). */
function _padHints(deck: OmnisDeck): ControlHint[] {
	const ch = omnisDuoPadChannel(deck);
	return HOT_CUE_SLOTS.map((slot, pad) => ({
		source: pioneerNote(ch, 8 + pad),
		label: `HOT CUE ${slot} + SHIFT (deck ${deck})`
	}));
}

function _globalHints(): ControlHint[] {
	return [
		// [PDF] 4-1 ROTARY SELECTOR press, + SHIFT.
		{ source: pioneerNote(BROWSER_CH, 51), label: 'BROWSE press' },
		{ source: pioneerNote(BROWSER_CH, 97), label: 'BROWSE press + SHIFT' },
		// [PDF] 2-1..2-3 Beat FX (all CC on channel 5).
		{ source: pioneerCc(MIXER_CH, 76), label: 'BEAT FX beat left' },
		{ source: pioneerCc(MIXER_CH, 120), label: 'BEAT FX beat left + SHIFT' },
		{ source: pioneerCc(MIXER_CH, 77), label: 'BEAT FX beat right' },
		{ source: pioneerCc(MIXER_CH, 121), label: 'BEAT FX beat right + SHIFT' },
		{ source: pioneerCc(MIXER_CH, 91), label: 'BEAT FX LEVEL/DEPTH' },
		{ source: pioneerCc(MIXER_CH, 114), label: 'BEAT FX ON/OFF' },
		{ source: pioneerCc(MIXER_CH, 52), label: 'BEAT FX ON/OFF + SHIFT' },
		// [PDF] 3-9 BOOTH MONITOR LEVEL, 3-11 MIC1/MIC2 LEVEL, 3-12 MIC ON, crossfader curve, mixer mode.
		{ source: pioneerCc(MIXER_CH, 25), label: 'BOOTH MONITOR LEVEL' },
		{ source: pioneerCc(MIXER_CH, 88), label: 'MIC1 LEVEL' },
		{ source: pioneerCc(MIXER_CH, 89), label: 'MIC2 LEVEL' },
		{ source: pioneerCc(MIXER_CH, 90), label: 'MIC ON' },
		{ source: pioneerCc(MIXER_CH, 95), label: 'CROSSFADER CURVE (settings)' },
		{ source: pioneerCc(MIXER_CH, 101), label: 'MIXER MODE (utility)' },
		// [PDF] 5-1..5-5 Bluetooth source section on channel 12.
		{ source: pioneerCc(BLUETOOTH_CH, 0), label: 'Bluetooth ON (pairing)' },
		{ source: pioneerCc(BLUETOOTH_CH, 1), label: 'Bluetooth headphone CUE' },
		{ source: pioneerNote(BLUETOOTH_CH, 0), label: 'Bluetooth PLAY/PAUSE' },
		{ source: pioneerNote(BLUETOOTH_CH, 1), label: 'Bluetooth TRACK FWD' },
		{ source: pioneerNote(BLUETOOTH_CH, 2), label: 'Bluetooth TRACK REV' }
	];
}

export const OMNIS_DUO_MAP: DeviceMap = {
	vendor: 'AlphaTheta',
	nameMatch: 'OMNIS-DUO',
	bindings: [
		...DECKS.flatMap((deck) => _deckBindings(deck)),
		...DECKS.flatMap((deck) => _mixerBindings(deck)),
		...DECKS.flatMap((deck) => _padBindings(deck)),
		..._globalBindings()
	],
	leds: DECKS.flatMap((deck) => _ledRules(deck)),
	meters: _meters(),
	hints: [
		...DECKS.flatMap((deck) => _deckHints(deck)),
		...DECKS.flatMap((deck) => _padHints(deck)),
		..._globalHints()
	]
};

/** Self-registration: maps/entries.ts discovers this export, so no shared list names this map. */
export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{ key: 'OMNIS_DUO_MAP', map: OMNIS_DUO_MAP, live: true }
];
