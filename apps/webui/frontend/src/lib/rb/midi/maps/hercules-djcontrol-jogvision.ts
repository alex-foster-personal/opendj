/**
 * Hercules DJControl Jogvision P0 device map (issue #10848).
 *
 * SOURCES (every wire number below is cited, none invented):
 *   [HC] "MIDI COMMANDS OF HERCULES DJCONTROL JOGVISION", Hercules DJ, Word 2010
 *        PDF (created 2016-01-29, 37 pages).
 *        https://ts.hercules.com/download/sound/manuals/DJC_jogvision/DJCJogvision_MIDI_Commands.pdf
 *        Support page: https://support.hercules.com/en/product/djcontroljogvision-en/
 *        sha256 a9ecc66294baaa6492ed812109335318bec386dabd4977d9c4ebfa7345f9baf7
 *        Sections: pp.1-10 Note On input; pp.11-18 CC input; pp.19-23 Aftertouch input;
 *        pp.23-30 Note output; pp.35-37 CC output.
 *
 * Clean room: Mixxx's GPL mapping was read only to cross-check wires already taken from [HC].
 * Nothing was copied from Mixxx or from tools/deck-diagrams/devices/hercules-djcontrol-jogvision/midi.json.
 *
 * hardware_confirmed: false (no hardware capture yet; export HERCULES_DJCONTROL_JOGVISION_HARDWARE_CONFIRMED).
 *
 * Scope notes:
 *   - SHIFT is encoded in firmware (SH_ rows send their own ids), so no `shift: true` bindings;
 *     the SHIFT button itself binds shift_modifier only so midiState.shiftHeld stays truthful
 *     and the practice replica can hold SHIFT (same ruling as ddj-400.ts, issue #13558).
 *   - Aftertouch (0xA0/0xA1 pad pressure) has no MidiSourceKind; rows are omitted.
 *   - Per-deck VU is note 0x44 velocity 0..6; MidiMeterOutput is CC-only, so no meter binding.
 *   - FX / loop / sampler / AIR rows are hints only: P0 MidiAction has no beat-FX action (same
 *     ruling as ddj-sx3.ts) and mixer_channel filter needs an absolute knob this unit lacks.
 *   - EQ and pitch fader orientation text in [HC] is ambiguous; no invert until hardware confirms.
 *
 * Requirements (mini-PRD):
 *   ✔︎ Transport, sync, PFL, hot cues 1-8, jog, pitch, fader, EQ, crossfader, browse, load.
 *     [if] note 0x32 ch1 arrives [then] deck_play_toggle deck 1
 *     [if] a binding cites an id absent from [HC] input tables [then ⛔️] fabricated wire
 */

import type {
	ControlHint,
	DeviceMap,
	DeviceMapEntry,
	LedRule,
	MidiBinding
} from '$lib/rb/midi/midi-types';
import type { HotCueSlot } from '$lib/rb/hot-cue-types';

/** No live hardware spot-check yet (issue #10848). */
export const HERCULES_DJCONTROL_JOGVISION_HARDWARE_CONFIRMED = false;

const HOT_SLOTS: readonly HotCueSlot[] = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];

function _deckBindings(ch: number, deck: 1 | 2): MidiBinding[] {
	const hot = HOT_SLOTS.map((slot, i): MidiBinding => ({
		// [HC p.1-10] CUE14_KP / CUE58_KP hot-cue pads 0x10-0x17.
		source: { ch, kind: 'note', id: 0x10 + i },
		action: { type: 'deck_hot_cue', deck, slot }
	}));
	return [
		// [HC p.1-10] PLAY 0x32.
		{ source: { ch, kind: 'note', id: 0x32 }, action: { type: 'deck_play_toggle', deck } },
		// [HC p.1-10] CUE 0x31.
		{ source: { ch, kind: 'note', id: 0x31 }, action: { type: 'deck_cue', deck } },
		// [HC p.1-10] SYNC 0x30.
		{ source: { ch, kind: 'note', id: 0x30 }, action: { type: 'deck_sync_toggle', deck } },
		// [HC p.1-10] LISTEN (PFL) 0x33.
		{ source: { ch, kind: 'note', id: 0x33 }, action: { type: 'channel_cue', deck } },
		// [HC p.1-10] LOAD 0x51.
		{ source: { ch, kind: 'note', id: 0x51 }, action: { type: 'browse_load', deck } },
		// [HC p.1-10] SHIFT 0x56 (modifier state only; shifted rows send their own ids).
		{ source: { ch, kind: 'note', id: 0x56 }, action: { type: 'shift_modifier' } },
		...hot,
		// [HC p.11-18] VOL 0x01 channel fader.
		{
			source: { ch, kind: 'cc', id: 0x01 },
			action: { type: 'mixer_channel', deck, target: 'fader' }
		},
		// [HC p.11-18] TREB / MED / BASS 0x03-0x05.
		{
			source: { ch, kind: 'cc', id: 0x03 },
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'high' }
		},
		{
			source: { ch, kind: 'cc', id: 0x04 },
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'mid' }
		},
		{
			source: { ch, kind: 'cc', id: 0x05 },
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'low' }
		},
		// [HC p.11-18] PITCH 0x02 MSB; LSB is CC 0x22 (PITCH_DA_LSB).
		{
			source: { ch, kind: 'cc', id: 0x02 },
			action: { type: 'deck_pitch', deck, lsbOffset: 0x20 }
		},
		// [HC p.11-18] JOG / JOG_SCRATCH 0x40 / 0x41: two's complement relative.
		{
			source: { ch, kind: 'cc', id: 0x40 },
			action: { type: 'deck_jog', deck },
			relative: true
		},
		{
			source: { ch, kind: 'cc', id: 0x41 },
			action: { type: 'deck_jog', deck },
			relative: true
		}
	];
}

const _GLOBAL_BINDINGS: MidiBinding[] = [
	// [HC p.11-18] XFADER 0x00 ch1 only.
	{
		source: { ch: 1, kind: 'cc', id: 0x00 },
		action: { type: 'mixer_global', target: 'crossfader' }
	},
	// [HC p.11-18] BROWSE_ENC 0x4e ch1 only.
	{ source: { ch: 1, kind: 'cc', id: 0x4e }, action: { type: 'browse_encoder' }, relative: true }
];

function _deckHints(ch: number, deck: 1 | 2): ControlHint[] {
	const d = `(deck ${deck})`;
	const note = (id: number, label: string): ControlHint => ({
		source: { ch, kind: 'note', id },
		label: `${label} ${d}`
	});
	const cc = (id: number, label: string): ControlHint => ({
		source: { ch, kind: 'cc', id },
		label: `${label} ${d}`
	});
	return [
		// [HC p.1-10] LOOP 0x00-0x02.
		note(0x00, 'LOOP 1'),
		note(0x01, 'LOOP 2'),
		note(0x02, 'LOOP 3'),
		// [HC p.1-10] AIR_FX 0x03.
		note(0x03, 'AIR FX'),
		// [HC p.1-10] FX 0x04-0x06.
		note(0x04, 'FX 1'),
		note(0x05, 'FX 2'),
		note(0x06, 'FX 3'),
		// [HC p.1-10] SH_LOOP 0x08-0x0a.
		note(0x08, 'SHIFT LOOP 1'),
		note(0x09, 'SHIFT LOOP 2'),
		note(0x0a, 'SHIFT LOOP 3'),
		// [HC p.1-10] SH_AIR / SH_FX 0x0b-0x0f.
		note(0x0b, 'SHIFT AIR FX'),
		note(0x0c, 'SHIFT FX 1'),
		note(0x0d, 'SHIFT FX 2'),
		note(0x0e, 'SHIFT FX 3'),
		note(0x0f, 'SHIFT AIR FX (FX)'),
		// [HC p.1-10] SH_CUE pads 0x18-0x1f.
		...Array.from({ length: 8 }, (_, i) => note(0x18 + i, `SHIFT hot cue pad ${i + 1}`)),
		// [HC p.1-10] SAM / SAM_BK 0x20-0x27.
		...Array.from({ length: 4 }, (_, i) => note(0x20 + i, `SAM pad ${i + 1}`)),
		...Array.from({ length: 4 }, (_, i) => note(0x24 + i, `SAM bank pad ${i + 1}`)),
		// [HC p.1-10] SH_SAM 0x28-0x2f.
		...Array.from({ length: 8 }, (_, i) => note(0x28 + i, `SHIFT SAM pad ${i + 1}`)),
		// [HC p.1-10] SLIP 0x34.
		note(0x34, 'SLIP'),
		// [HC p.1-10] SH transport 0x35-0x39.
		note(0x35, 'SHIFT SYNC'),
		note(0x36, 'SHIFT CUE'),
		note(0x37, 'SHIFT PLAY'),
		note(0x38, 'SHIFT LISTEN'),
		note(0x39, 'SHIFT SLIP'),
		// [HC p.1-10] MULTI_FX 0x50.
		note(0x50, 'MULTI FX'),
		// [HC p.1-10] SH_MULTI_FX 0x52, FX 0x54.
		note(0x52, 'SHIFT MULTI FX'),
		note(0x53, 'SHIFT LOAD'),
		note(0x54, 'FX'),
		// [HC p.1-10] JOG_TOUCH 0x55, MODE 0x57 (SHIFT 0x56 is bound).
		note(0x55, 'JOG touch'),
		note(0x57, 'MODE'),
		// [HC p.11-18] AIR_CONTROL 0x0f (IR sensor).
		cc(0x0f, 'Air control'),
		// [HC p.11-18] VOL_LSB 0x21 (14-bit consumed on bound VOL MSB).
		cc(0x21, 'VOLUME LSB'),
		// [HC p.11-18] EQ LSBs 0x23-0x25.
		cc(0x23, 'TREB LSB'),
		cc(0x24, 'MED LSB'),
		cc(0x25, 'BASS LSB'),
		// [HC p.11-18] SH_JOG 0x42-0x43.
		cc(0x42, 'SHIFT JOG'),
		cc(0x43, 'SHIFT JOG SCRATCH'),
		// [HC p.11-18] LOOP_ENC 0x44-0x45.
		cc(0x44, 'LOOP encoder'),
		cc(0x45, 'LOOP encoder 2'),
		// [HC p.11-18] FX_ENC 0x46-0x47.
		cc(0x46, 'FX encoder'),
		cc(0x47, 'FX encoder 2'),
		// [HC p.11-18] unnamed 0x48-0x49.
		cc(0x48, 'unnamed chart row 0x48'),
		cc(0x49, 'unnamed chart row 0x49'),
		// [HC p.11-18] SH_LOOP_ENC / SH_FX_ENC 0x4a-0x4d.
		cc(0x4a, 'SHIFT LOOP encoder'),
		cc(0x4b, 'SHIFT LOOP encoder 2'),
		cc(0x4c, 'SHIFT FX encoder'),
		cc(0x4d, 'SHIFT FX encoder 2'),
		// [HC p.11-18] SH+BROWSE_ENC 0x4f on deck channel.
		cc(0x4f, 'SHIFT browse encoder'),
		// [HC p.11-18] KPAD_MODE 0x50.
		cc(0x50, 'Keypad mode')
	];
}

const _GLOBAL_HINTS: ControlHint[] = [
	// [HC p.1-10] ch1-only notes 0x45-0x4f.
	{ source: { ch: 1, kind: 'note', id: 0x45 }, label: 'VINYL' },
	{ source: { ch: 1, kind: 'note', id: 0x46 }, label: 'unnamed chart row 0x46 (deck 1)' },
	{ source: { ch: 1, kind: 'note', id: 0x47 }, label: 'VIEW' },
	{ source: { ch: 1, kind: 'note', id: 0x48 }, label: 'LOAD PREPARE' },
	{ source: { ch: 1, kind: 'note', id: 0x49 }, label: 'SHIFT VINYL' },
	{ source: { ch: 1, kind: 'note', id: 0x4a }, label: 'SHIFT VIEW' },
	{ source: { ch: 1, kind: 'note', id: 0x4b }, label: 'SHIFT LOAD PREPARE' },
	{ source: { ch: 1, kind: 'note', id: 0x4c }, label: 'HP MIX' },
	{ source: { ch: 1, kind: 'note', id: 0x4d }, label: 'HP CUE' },
	{ source: { ch: 1, kind: 'note', id: 0x4e }, label: 'MIC' },
	{ source: { ch: 1, kind: 'note', id: 0x4f }, label: 'BROWSE button' },
	// [HC p.11-18] XFADER_LSB 0x20, CUE_MIX_MODE 0x51.
	{ source: { ch: 1, kind: 'cc', id: 0x20 }, label: 'CROSSFADER LSB' },
	{ source: { ch: 1, kind: 'cc', id: 0x51 }, label: 'CUE MIX MODE' }
];

function _deckLeds(ch: number, deck: 1 | 2): LedRule[] {
	const vel = { velocityOn: 0x7f, velocityOff: 0x00 };
	const out = (note: number) => ({ ch, note, ...vel });
	const slots = HOT_SLOTS;
	const hot = slots.map((slot, i) => ({
		trigger: { kind: 'hot_cue_present' as const, deck, slot },
		out: out(0x10 + i)
	}));
	return [
		// [HC p.23-30] PLAY / CUE / SYNC / LISTEN output matches input ids.
		{ trigger: { kind: 'deck_playing', deck }, out: out(0x32) },
		{ trigger: { kind: 'beat_sync_enabled', deck }, out: out(0x30) },
		{ trigger: { kind: 'channel_cue_enabled', deck }, out: out(0x33) },
		...hot
	];
}

export const HERCULES_DJCONTROL_JOGVISION_MAP: DeviceMap = {
	vendor: 'Hercules',
	nameMatch: 'DJControl Jogvision',
	bindings: [..._deckBindings(1, 1), ..._deckBindings(2, 2), ..._GLOBAL_BINDINGS],
	leds: [..._deckLeds(1, 1), ..._deckLeds(2, 2)],
	hints: [..._deckHints(1, 1), ..._deckHints(2, 2), ..._GLOBAL_HINTS]
};

/** Self-registration: maps/entries.ts discovers this export, so no shared list names this map. */
export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{ key: 'HERCULES_DJCONTROL_JOGVISION_MAP', map: HERCULES_DJCONTROL_JOGVISION_MAP, live: true }
];
