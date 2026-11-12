/**
 * Hercules P32 DJ map.
 *
 * Wire source (clean room): Hercules "P32 DJ MIDI COMMAND LIST" Rev1.0, from the
 * product support page https://support.hercules.com/en/product/p32dj-en/ (section
 * "MIDI mapping"), download
 * https://ts.hercules.com/download/sound/manuals/P32DJ/Hercules_P32_DJ_MIDI_Command_List.zip
 * (zip sha256 d7f3ef73a80d13d6a6f78ca3e3c816a6e24e5e3d4d5929b86cf31e2938099cc7;
 * Hercules_P32_DJ_MIDI_Command_List_rev1.0.pdf sha256
 * a61480e15fbfdafeaeeb756d60835347852a84fb7d6dc6cea1440867788c028a).
 * Sections: GLOBAL MIXER / GLOBAL MIXER + SHIFT p.2; DECK COMMAND LIST MIDI INPUT
 * p.3-5, MIDI OUTPUT p.5-7; Manage Pads Colors p.8. Cross-checked against
 * "HERCULES P32 DJ-MIDI-Layout1.7.pdf". No wire comes from Mixxx or from
 * tools/deck-diagrams/devices/hercules-p32-dj/midi.json.
 *
 * Hardware confirmed: no (hardware_confirmed: false).
 *
 * Channels (1-based): 1 global, 2 deck A, 3 deck B, 4 global + SHIFT,
 * 5 deck A + SHIFT, 6 deck B + SHIFT. SHIFT is a hardware modifier.
 *
 * Scope notes: the chart has no jog wheel or pitch fader row (the hardware has
 * neither), so no deck_jog / deck_pitch is bound. FX, FILTER (a relative encoder)
 * and LOOP_ENC are learn-log hints because the MIDI action contract has no FX
 * action yet.
 */

import type {
	ControlHint,
	DeviceMap,
	DeviceMapEntry,
	LedRule,
	MidiBinding
} from '$lib/rb/midi/midi-types';
import type { HotCueSlot } from '$lib/rb/hot-cue-types';

type P32Deck = 1 | 2;

const HOT_CUE_SLOTS: readonly HotCueSlot[] = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];
const NO_FX = 'no FX action in the MIDI contract yet';

const deckChannel = (deck: P32Deck): number => deck + 1;
const shiftChannel = (deck: P32Deck): number => deck + 4;

function _deckBindings(deck: P32Deck): MidiBinding[] {
	const ch = deckChannel(deck);
	return [
		// [chart p.3] PLAY / CUE / SYNC notes 0x0A / 0x09 / 0x08
		{ source: { ch, kind: 'note', id: 0x0a }, action: { type: 'deck_play_toggle', deck } },
		{ source: { ch, kind: 'note', id: 0x09 }, action: { type: 'deck_cue', deck } },
		{ source: { ch, kind: 'note', id: 0x08 }, action: { type: 'deck_sync_toggle', deck } },
		// [chart p.3] LOOP_BTN note 0x01
		{ source: { ch, kind: 'note', id: 0x01 }, action: { type: 'deck_auto_loop_toggle', deck } },
		// [chart p.3] PFL note 0x10, LOAD note 0x0F
		{ source: { ch, kind: 'note', id: 0x10 }, action: { type: 'channel_cue', deck } },
		{ source: { ch, kind: 'note', id: 0x0f }, action: { type: 'browse_load', deck } },
		// [chart p.3] VOL cc 0x01, LOW/MID/HIGH cc 0x02/0x03/0x04
		{
			source: { ch, kind: 'cc', id: 0x01 },
			action: { type: 'mixer_channel', deck, target: 'fader' }
		},
		{
			source: { ch, kind: 'cc', id: 0x02 },
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'low' }
		},
		{
			source: { ch, kind: 'cc', id: 0x03 },
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'mid' }
		},
		{
			source: { ch, kind: 'cc', id: 0x04 },
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'high' }
		},
		// [chart p.4] HOTCUE mode P1..P8 notes 0x54..0x5B
		...HOT_CUE_SLOTS.map((slot, index): MidiBinding => ({
			source: { ch, kind: 'note', id: 0x54 + index },
			action: { type: 'deck_hot_cue', deck, slot }
		}))
	];
}

const GLOBAL_BINDINGS: MidiBinding[] = [
	// [chart p.2] XFADER B0 01
	{
		source: { ch: 1, kind: 'cc', id: 0x01 },
		action: { type: 'mixer_global', target: 'crossfader' }
	},
	// [chart p.2] BROWSE_ENC B0 02, 01..3F CW / 7F..40 CCW
	{ source: { ch: 1, kind: 'cc', id: 0x02 }, action: { type: 'browse_encoder' }, relative: true }
];

function _deckHints(deck: P32Deck): ControlHint[] {
	const ch = deckChannel(deck);
	const sh = shiftChannel(deck);
	const d = `deck ${deck}`;
	const hints: ControlHint[] = [
		{ source: { ch, kind: 'cc', id: 0x05 }, label: `FILTER (${d}; ${NO_FX})` },
		{ source: { ch, kind: 'cc', id: 0x06 }, label: `FX1 LEVEL (${d}; ${NO_FX})` },
		{ source: { ch, kind: 'cc', id: 0x07 }, label: `FX2 LEVEL (${d}; ${NO_FX})` },
		{ source: { ch, kind: 'cc', id: 0x08 }, label: `FX3 LEVEL (${d}; ${NO_FX})` },
		{ source: { ch, kind: 'cc', id: 0x09 }, label: `DRY/WET (${d}; ${NO_FX})` },
		{ source: { ch, kind: 'cc', id: 0x0a }, label: `LOOP encoder (${d}; not available)` },
		{ source: { ch, kind: 'note', id: 0x02 }, label: `FILTER ON (${d}; ${NO_FX})` },
		{ source: { ch, kind: 'note', id: 0x03 }, label: `FX1 ON (${d}; ${NO_FX})` },
		{ source: { ch, kind: 'note', id: 0x04 }, label: `FX2 ON (${d}; ${NO_FX})` },
		{ source: { ch, kind: 'note', id: 0x05 }, label: `FX3 ON (${d}; ${NO_FX})` },
		{ source: { ch, kind: 'note', id: 0x06 }, label: `MACRO FX ON (${d}; ${NO_FX})` },
		{ source: { ch, kind: 'note', id: 0x07 }, label: `SHIFT hold (${d}; hardware modifier)` }
	];
	for (let m = 0; m < 4; m++)
		hints.push({
			source: { ch, kind: 'note', id: 0x0b + m },
			label: `MODE${m + 1} pad mode (${d}; hardware-routed)`
		});
	for (let p = 9; p <= 16; p++)
		hints.push({
			source: { ch, kind: 'note', id: 0x54 + p - 1 },
			label: `HOTCUE pad ${p} (${d}; not available)`
		});
	for (const [mode, base] of [
		['LOOP', 0x44],
		['SLICER', 0x34],
		['SAMPLER', 0x24]
	] as const)
		for (let p = 1; p <= 16; p++)
			hints.push({
				source: { ch, kind: 'note', id: base + p - 1 },
				label: `${mode} pad ${p} (${d}; not available)`
			});
	for (const [id, name] of [
		[0x01, 'VOL'],
		[0x02, 'LOW'],
		[0x03, 'MID'],
		[0x04, 'HIGH']
	] as const)
		hints.push({
			source: { ch: sh, kind: 'cc', id },
			label: `${name} with SHIFT held (${d}; not bound)`
		});
	return hints;
}

const GLOBAL_HINTS: ControlHint[] = [
	{ source: { ch: 1, kind: 'note', id: 0x01 }, label: 'BROWSE press (not available)' },
	{ source: { ch: 1, kind: 'note', id: 0x02 }, label: 'REC (not available)' },
	{ source: { ch: 1, kind: 'note', id: 0x03 }, label: 'SLIP (not available)' },
	{ source: { ch: 1, kind: 'note', id: 0x04 }, label: 'VOL UP (not available)' },
	{ source: { ch: 1, kind: 'note', id: 0x05 }, label: 'VOL DOWN (not available)' },
	{ source: { ch: 4, kind: 'cc', id: 0x02 }, label: 'SHIFT + BROWSE turn (not available)' },
	{ source: { ch: 4, kind: 'note', id: 0x01 }, label: 'SHIFT + BROWSE press (not available)' }
];

function _deckLeds(deck: P32Deck): LedRule[] {
	const ch = deckChannel(deck);
	const out = (note: number) => ({ ch, note, velocityOn: 0x7f, velocityOff: 0x00 });
	return [
		{ trigger: { kind: 'deck_playing', deck }, out: out(0x0a) },
		{ trigger: { kind: 'beat_sync_enabled', deck }, out: out(0x08) },
		{ trigger: { kind: 'channel_cue_enabled', deck }, out: out(0x10) },
		...HOT_CUE_SLOTS.map((slot, index): LedRule => ({
			trigger: { kind: 'hot_cue_present', deck, slot },
			out: out(0x54 + index)
		}))
	];
}

export const HERCULES_P32_DJ_MAP: DeviceMap = {
	vendor: 'Hercules',
	nameMatch: '\\bP32\\s*DJ\\b',
	bindings: [..._deckBindings(1), ..._deckBindings(2), ...GLOBAL_BINDINGS],
	leds: [..._deckLeds(1), ..._deckLeds(2)],
	hints: [..._deckHints(1), ..._deckHints(2), ...GLOBAL_HINTS]
};

/** Self-registration: maps/entries.ts discovers this export, so no shared list names this map. */
export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{ key: 'HERCULES_P32_DJ_MAP', map: HERCULES_P32_DJ_MAP, live: true }
];
