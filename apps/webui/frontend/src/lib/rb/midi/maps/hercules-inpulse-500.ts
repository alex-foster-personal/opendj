/**
 * Hercules DJControl Inpulse 500 live device map (catalog id hercules-inpulse-500).
 *
 * SOURCE OF TRUTH for EVERY Note/CC number in this file: Hercules
 * "MIDI COMMANDS", DJControl Inpulse 500, (c) 2022 Guillemot:
 * https://ts.hercules.com/download/sound/manuals/DJC_Inpulse500/DJControlInpulse500_MIDI_Commands.pdf
 * Sections: DECK 1 (D1-1..D1-20), DECK 1 PADS (P1-1..P1-12), DECK 2 (D2-1..D2-20),
 * DECK 2 PADS (P2-1..P2-12), MIXER (M-1..M-32), BROWSER (B1..B4), MIDI OUT, pad colours.
 * Chart decimal Channel column is 0-based; MidiSource.ch is 1-based (hex status nibble + 1).
 *
 * hardware_confirmed: false. No unit on this issue; pitch polarity uninverted until capture.
 *
 * DO NOT source numbers from tools/deck-diagrams/devices/hercules-inpulse-500/midi.json
 * or from Mixxx (GPL). Clean-room chart only.
 *
 * Encoders use two's complement relative ticks (7F>40 CCW, 01>3F CW): `relative: true`
 * without `relativeCentered` (not centred on 0x40).
 *
 * Jog touch (Toggle) prints B1 08 / B2 08, the same wire as Tempo Fader MSB — touch is
 * unbound and unhinted; capture would show whether touch is really 9x 08.
 *
 * Shift is hardware-encoded (shifted deck rows move to ch 5/6, global/browser to ch 4);
 * no `shift_modifier` binding. Shift buttons are hints only.
 *
 * Chart misprints (not bound or hinted): M-3 Deck 1 vol LSB B1 00; shifted vol rows
 * B4 20 vs deck 2 B5 00/20; Deck 2 Mode 4A long press 91 43 (deck 1 channel).
 * M-6 Guides and B4 Assistant both 90 01 — hinted once as Assistant / Guides.
 *
 * FX: colour-FX analog is each deck's Filter knob (`mixer_channel` `filter`). Beat Fx1-4
 * are hints only until #9788 adds a beat-FX action. Pads Modes 2-8 are unbound/unhinted.
 *
 * Mode 1 pads bind as hot cues A-H (software choice; chart names only "Mode 1").
 */

import type {
	ControlHint,
	DeviceMap,
	DeviceMapEntry,
	LedRule,
	MidiBinding,
	MidiMeterOutput,
	MidiSource
} from '$lib/rb/midi/midi-types';
import type { HotCueSlot } from '$lib/rb/hot-cue-types';

const DECKS = [1, 2] as const;
type Deck = (typeof DECKS)[number];
const CH_GLOBAL = 1;
const CH_SHIFT_BROWSER = 4;
const HOT_CUE_SLOTS: readonly HotCueSlot[] = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];

const deckCh = (deck: Deck): number => deck + 1;
const padCh = (deck: Deck): number => deck + 6;
const shiftDeckCh = (deck: Deck): number => deck + 4;

const note = (ch: number, id: number): MidiSource => ({ ch, kind: 'note', id });
const cc = (ch: number, id: number): MidiSource => ({ ch, kind: 'cc', id });

/** [PDF] MIDI OUT / pad colour table: 7F on, 00 off. White velocity for hot-cue LEDs. */
export const INPULSE_500_LED_ON = 0x7f;
export const INPULSE_500_LED_OFF = 0x00;

function _jog(deck: Deck, id: number): MidiBinding {
	return {
		source: cc(deckCh(deck), id),
		action: { type: 'deck_jog', deck },
		relative: true
	};
}

function _deckBindings(deck: Deck): MidiBinding[] {
	const ch = deckCh(deck);
	return [
		// [PDF] D1-1 / D2-1 Play: 91 07 / 92 07.
		{ source: note(ch, 0x07), action: { type: 'deck_play_toggle', deck } },
		// [PDF] D1-2 / D2-2 Cue: 91 06 / 92 06.
		{ source: note(ch, 0x06), action: { type: 'deck_cue', deck } },
		// [PDF] D1-10 / D2-10 Sync: 91 05 / 92 05.
		{ source: note(ch, 0x05), action: { type: 'deck_sync_toggle', deck } },
		// [PDF] D1-4 / D2-4 In: 91 09 / 92 09.
		{ source: note(ch, 0x09), action: { type: 'deck_manual_loop_cycle', deck } },
		// [PDF] D1-5 / D2-5 Out: 91 0A / 92 0A.
		{ source: note(ch, 0x0a), action: { type: 'deck_loop_exit', deck } },
		// [PDF] D1-6 / D2-6 Autoloop push: 91 2C / 92 2C.
		{ source: note(ch, 0x2c), action: { type: 'deck_auto_loop_toggle', deck } },
		// [PDF] D1-15 / D2-15 Tempo Fader MSB: B1 08 / B2 08 (LSB B1 28 / B2 28).
		{ source: cc(ch, 0x08), action: { type: 'deck_pitch', deck, lsbOffset: 32 } },
		// [PDF] D1-16 / D2-16 Jog-wheel: B1 09 / B2 09.
		_jog(deck, 0x09),
		// [PDF] D1-16 / D2-16 Jog-wheel Vinyl: B1 0A / B2 0A.
		_jog(deck, 0x0a),
		// [PDF] B1 / B2 Load: 91 0D / 92 0D.
		{ source: note(ch, 0x0d), action: { type: 'browse_load', deck } },
		// [PDF] M-5 / M-7 PFL: 91 0C / 92 0C.
		{ source: note(ch, 0x0c), action: { type: 'channel_cue', deck } }
	];
}

function _mixerBindings(deck: Deck): MidiBinding[] {
	const ch = deckCh(deck);
	return [
		// [PDF] M-3 / M-4 Deck vol MSB: B1 00 / B2 00.
		{ source: cc(ch, 0x00), action: { type: 'mixer_channel', deck, target: 'fader' } },
		// [PDF] M-8 / M-9 Filter MSB (colour-FX analog): B1 01 / B2 01.
		{ source: cc(ch, 0x01), action: { type: 'mixer_channel', deck, target: 'filter' } },
		// [PDF] M-14 / M-15 Low MSB: B1 02 / B2 02.
		{
			source: cc(ch, 0x02),
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'low' }
		},
		// [PDF] M-16 / M-17 Mid MSB: B1 03 / B2 03.
		{
			source: cc(ch, 0x03),
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'mid' }
		},
		// [PDF] M-18 / M-19 Hi MSB: B1 04 / B2 04.
		{
			source: cc(ch, 0x04),
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'high' }
		},
		// [PDF] M-20 / M-21 Gain MSB: B1 05 / B2 05.
		{ source: cc(ch, 0x05), action: { type: 'mixer_channel', deck, target: 'trim' } }
	];
}

function _padBindings(deck: Deck): MidiBinding[] {
	const pch = padCh(deck);
	// [PDF] P1-1..P1-8 / P2-1..P2-8 Mode 1 pads: ch 7/8 notes 0x00..0x07.
	return HOT_CUE_SLOTS.map((slot, i) => ({
		source: note(pch, i),
		action: { type: 'deck_hot_cue', deck, slot }
	}));
}

function _globalBindings(): MidiBinding[] {
	return [
		// [PDF] M-1 Xfader MSB: B0 00.
		{ source: cc(CH_GLOBAL, 0x00), action: { type: 'mixer_global', target: 'crossfader' } },
		// [PDF] M-28 Master vol MSB: B0 03.
		{ source: cc(CH_GLOBAL, 0x03), action: { type: 'mixer_global', target: 'master' } },
		// [PDF] M-32 Headphones vol MSB: B0 04.
		{ source: cc(CH_GLOBAL, 0x04), action: { type: 'headphone_level' } },
		// [PDF] M-31 Cue-Master MSB: B0 05.
		{ source: cc(CH_GLOBAL, 0x05), action: { type: 'headphone_mix' } },
		// [PDF] M-30 Master pfl: 90 02.
		{ source: note(CH_GLOBAL, 0x02), action: { type: 'master_cue', mode: 'latch' } },
		// [PDF] B3 Browse rotary: B0 01.
		{
			source: cc(CH_GLOBAL, 0x01),
			action: { type: 'browse_encoder' },
			relative: true
		}
	];
}

function _led(noteCh: number, noteId: number, trigger: LedRule['trigger']): LedRule {
	return {
		trigger,
		out: {
			ch: noteCh,
			note: noteId,
			velocityOn: INPULSE_500_LED_ON,
			velocityOff: INPULSE_500_LED_OFF
		}
	};
}

function _ledRules(deck: Deck): LedRule[] {
	const ch = deckCh(deck);
	const pch = padCh(deck);
	return [
		// [PDF] MIDI OUT Play LED: 91 07 / 92 07.
		_led(ch, 0x07, { kind: 'deck_playing', deck }),
		// [PDF] Cue LED: 91 06 — software choice: lit when loaded (RMX 2 precedent).
		_led(ch, 0x06, { kind: 'deck_loaded', deck }),
		// [PDF] Sync LED: 91 05.
		_led(ch, 0x05, { kind: 'beat_sync_enabled', deck }),
		// [PDF] PFL LED: 91 0C.
		_led(ch, 0x0c, { kind: 'channel_cue_enabled', deck }),
		// [PDF] Mode 1 pad LEDs ch 7/8 notes 0x00..0x07, White (0x7F) on.
		...HOT_CUE_SLOTS.map((slot, i) => _led(pch, i, { kind: 'hot_cue_present', deck, slot }))
	];
}

function _deckHints(deck: Deck): ControlHint[] {
	const ch = deckCh(deck);
	const pch = padCh(deck);
	const shCh = shiftDeckCh(deck);
	const d = `(deck ${deck})`;
	const modeLongPress = deck === 1 ? [0x40, 0x41, 0x42, 0x43] : [0x40, 0x41, 0x42];
	return [
		// [PDF] D1-3 / D2-3 Slip: 91 01 / 92 01.
		{ source: note(ch, 0x01), label: `Slip ${d}` },
		// [PDF] D1-7 / D2-7 Quant: 91 02 / 92 02.
		{ source: note(ch, 0x02), label: `Quant ${d}` },
		// [PDF] D1-8 / D2-8 Vinyl: 91 03 / 92 03.
		{ source: note(ch, 0x03), label: `Vinyl ${d}` },
		// [PDF] D1-9 / D2-9 Shift: 91 04 / 92 04.
		{ source: note(ch, 0x04), label: `Shift ${d}` },
		// [PDF] D1-4 In long press: 91 0B / 92 0B.
		{ source: note(ch, 0x0b), label: `In (long press) ${d}` },
		// [PDF] B1/B2 Load long press: 91 0E / 92 0E.
		{ source: note(ch, 0x0e), label: `Load (long press) ${d}` },
		// [PDF] D1-6 Autoloop rotary: B1 0E / B2 0E.
		{ source: cc(ch, 0x0e), label: `Autoloop rotary ${d}` },
		// [PDF] D1-11..D1-14 Mode 1..4: 91 0F..12.
		...([0x0f, 0x10, 0x11, 0x12] as const).map((id, i) => ({
			source: note(ch, id),
			label: `Mode ${i + 1} ${d}`
		})),
		// [PDF] Mode 5..8 (+Shift): 91 13..16.
		...([0x13, 0x14, 0x15, 0x16] as const).map((id, i) => ({
			source: note(ch, id),
			label: `Mode ${i + 5} (+Shift) ${d}`
		})),
		// [PDF] Mode 1A..4A long press (deck 2 omits 4A per chart misprint).
		...modeLongPress.map((id, i) => ({
			source: note(ch, id),
			label: `Mode ${i + 1}A (long press) ${d}`
		})),
		// [PDF] P1 Shift+Pad 1..8 Mode 1: ch 7/8 notes 0x08..0x0F.
		...([0x08, 0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x0e, 0x0f] as const).map((id, i) => ({
			source: note(pch, id),
			label: `Shift + Pad ${i + 1} (Mode 1) ${d}`
		})),
		// [PDF] Shifted transport deck 1 ch 5 / deck 2 ch 6.
		{ source: note(shCh, 0x07), label: `Shift + Play ${d}` },
		{ source: note(shCh, 0x06), label: `Shift + Cue ${d}` },
		{ source: note(shCh, 0x05), label: `Shift + Sync ${d}` },
		{ source: note(shCh, 0x0d), label: `Shift + Load ${d}` }
	];
}

function _globalHints(): ControlHint[] {
	const fxNotes = [0x14, 0x15, 0x16, 0x17] as const;
	return [
		// [PDF] B4 Assistant / M-6 Guides: 90 01.
		{ source: note(CH_GLOBAL, 0x01), label: 'Assistant / Guides' },
		// [PDF] B4 Browse push: 90 00.
		{ source: note(CH_GLOBAL, 0x00), label: 'Browse push' },
		// [PDF] B4 Browse + Assistant: B0 02.
		{ source: cc(CH_GLOBAL, 0x02), label: 'Browse + Assistant' },
		// [PDF] M-2 Fx1..Fx4: 90 14..17.
		...fxNotes.map((id, i) => ({ source: note(CH_GLOBAL, id), label: `Fx${i + 1}` })),
		// [PDF] M-29 Xfader curve on/off: 90 18.
		{ source: note(CH_GLOBAL, 0x18), label: 'Xfader curve on/off' },
		// [PDF] M-29 Xfader curve (Mix/Scratch): B0 0B.
		{ source: cc(CH_GLOBAL, 0x0b), label: 'Xfader curve (Mix/Scratch)' },
		// [PDF] M-23 Mic vol: B0 06.
		{ source: cc(CH_GLOBAL, 0x06), label: 'Mic vol' },
		// [PDF] M-24 Mic hi: B0 07.
		{ source: cc(CH_GLOBAL, 0x07), label: 'Mic hi' },
		// [PDF] M-25 Mic low: B0 08.
		{ source: cc(CH_GLOBAL, 0x08), label: 'Mic low' },
		// [PDF] M-26 Aux vol: B0 09.
		{ source: cc(CH_GLOBAL, 0x09), label: 'Aux vol' },
		// [PDF] M-27 Aux filter: B0 0A.
		{ source: cc(CH_GLOBAL, 0x0a), label: 'Aux filter' },
		// [PDF] Shifted browser ch 4.
		{ source: cc(CH_SHIFT_BROWSER, 0x01), label: 'Shift + Browse' },
		{ source: note(CH_SHIFT_BROWSER, 0x00), label: 'Shift + Browse push' },
		{ source: note(CH_SHIFT_BROWSER, 0x02), label: 'Shift + Master pfl' },
		...fxNotes.map((id, i) => ({
			source: note(CH_SHIFT_BROWSER, id),
			label: `Shift + Fx${i + 1}`
		}))
	];
}

/** [PDF] M-22 Deck VU MIDI OUT: B1 40 / B2 40, 00..7F (9 segments). */
export const INPULSE_500_METERS: readonly MidiMeterOutput[] = DECKS.map((deck) => ({
	deck,
	out: { ch: deckCh(deck), cc: 0x40, maxValue: 0x7f }
}));

export const HERCULES_INPULSE_500_MAP: DeviceMap = {
	vendor: 'Hercules',
	nameMatch: 'Inpulse\\s*500',
	bindings: [
		...DECKS.flatMap((deck) => _deckBindings(deck)),
		...DECKS.flatMap((deck) => _mixerBindings(deck)),
		...DECKS.flatMap((deck) => _padBindings(deck)),
		..._globalBindings()
	],
	leds: DECKS.flatMap((deck) => _ledRules(deck)),
	meters: [...INPULSE_500_METERS],
	hints: [...DECKS.flatMap((deck) => _deckHints(deck)), ..._globalHints()]
};

/** Self-registration: maps/entries.ts discovers this export, so no shared list names this map. */
export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{ key: 'HERCULES_INPULSE_500_MAP', map: HERCULES_INPULSE_500_MAP, live: true }
];
