/**
 * Pioneer DJ XDJ-RX3 and AlphaTheta XDJ-AZ device maps.
 *
 * SOURCE OF TRUTH for every number: the official MIDI message lists (not
 * redistributed; sha256 in docs/controller/research/XDJ-MIDI-Message-Lists.md).
 * Mixxx ships no mapping for either unit. The unmerged XDJ-RX mapping
 * (mixxxdj/mixxx#3008) sets the BEHAVIOUR reference only: its jog and tempo
 * wires do not exist on these units.
 *
 * NOT HARDWARE CONFIRMED. Neither unit has been connected to this code.
 *
 * Channel layout: deck n -> channel n; browser -> 1; mixer and effects -> 5.
 *   RX3 pads: deck n -> channel 5 + n.
 *   AZ pads:  pads 1-4 -> channel 5 + n, pads 5-8 -> channel 9 + n.
 *
 * CH CUE and MASTER CUE are CCs on both units, hence ccButton.
 * The RX3 list gives SHIFT no wire: it is hardware-encoded.
 * The AZ list prints its CH FADER rows as "Upper Side=0", against the RX3 and
 * every other Pioneer list. Read here as lower = 0; check at soundcheck.
 *
 * Jog rotation centres on 64 on both units, hence relativeCentered. Paused it
 * scrubs, playing it bends the tempo. There is no scratch: jog touch is unbound.
 *
 * OUT OF CONTRACT (hints only): jog touch, needle search, track and fast search,
 * slip, reverse, quantize, Beat FX. No MidiAction exists for them.
 */

import type { ControlHint, DeviceMap, LedRule, MidiBinding } from '$lib/rb/midi/midi-types';
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
		{ source: pioneerNote(ch, 6), action: { type: 'deck_manual_loop_cycle', deck } },
		{ source: pioneerNote(ch, 7), action: { type: 'deck_manual_loop_cycle', deck } },
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

function _xdjMap(nameMatch: string, padWire: PadWires, extra: MidiBinding[]): DeviceMap {
	return {
		vendor: 'Pioneer DJ',
		nameMatch,
		bindings: [
			...DECKS.flatMap((deck) => _deckBindings(deck)),
			...DECKS.flatMap((deck) => _mixerBindings(deck)),
			...DECKS.flatMap((deck) => _padBindings(deck, padWire)),
			..._globalBindings(),
			...extra
		],
		leds: DECKS.flatMap((deck) => _ledRules(deck, padWire)),
		hints: DECKS.flatMap((deck) => _deckHints(deck))
	};
}

function _azOnlyBindings(deck: DeckId): MidiBinding[] {
	const ch = pioneerDeckChannel(deck);
	return [
		{ source: pioneerNote(ch, 63), action: { type: 'shift_modifier' } },
		{ source: pioneerNote(ch, 20), action: { type: 'deck_key_sync_toggle', deck } },
		{ source: pioneerNote(ch, 67), action: { type: 'deck_beat_loop', deck, beats: 4 } },
		{ source: pioneerNote(ch, 68), action: { type: 'deck_beat_loop', deck, beats: 8 } }
	];
}

export const XDJ_RX3_MAP: DeviceMap = _xdjMap('XDJ-RX3', rx3PadWire, []);
export const XDJ_AZ_MAP: DeviceMap = _xdjMap(
	'XDJ-AZ',
	azPadWire,
	DECKS.flatMap((deck) => _azOnlyBindings(deck))
);
