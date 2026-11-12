/**
 * Hercules DJ Console RMX Advanced live MIDI map (issue #10836).
 *
 * SOURCE OF TRUTH for every (channel, kind, id): the manufacturer's published
 * "MIDI messages sent by DJ Console Rmx" table in the Hercules DJ Console RMX
 * User Manual (EN, 32 pages), section 10. MIDI STANDARD SUPPORT, pages 24-25:
 *   https://www.manualshelf.com/manual/hercules/dj-console-rmx/operation-manual-english/page-24.html
 *   https://www.manualshelf.com/manual/hercules/dj-console-rmx/operation-manual-english/page-25.html
 * Received / LED table (page 26; not used for output here):
 *   https://www.manualshelf.com/manual/hercules/dj-console-rmx/operation-manual-english/page-26.html
 * Same document indexed at https://www.manua.ls/hercules/dj-console-rmx/manual
 *
 * Mixxx's mapping is GPL and this repo is Apache 2.0: wires are transcribed
 * from the manufacturer sent table only. Do not source numbers from Mixxx XML
 * or from tools/deck-diagrams/devices/hercules-dj-console-rmx-advanced/midi.json.
 *
 * NOT HARDWARE CONFIRMED.
 *
 * Channel layout: every sent row is Bx <cc> Value. x is the device MIDI
 * channel nibble (factory default 0 = channel 1). Left and right decks share
 * one channel and are distinguished by CC number (Left Play 0x0B, Right Play
 * 0x23). Kind is cc for every control; the sent table has no Note or Pitch
 * Bend rows.
 *
 * Buttons send CC 7F pressed / 00 released, so every button action sets
 * ccButton: true. Jogs are two's-complement relative (7F>40 CCW Slow>Fast,
 * 01>3F CW Slow>Fast), so relative: true and not relativeCentered.
 *
 * The chart names no Filter, Colour FX, Beat FX, or FX knob. Keypads 1-6
 * per deck are the unlabeled multi-function pads and bind as controller_pad.
 *
 * LED rules are omitted: page 26 lights Play/Cue/Sync/etc. with the same CC
 * numbers, and LedRule.out sends Note On, which would be the wrong kind.
 *
 * OUT OF CONTRACT (hints only): Beatlock, Previous/Next, Stop, Kill EQ,
 * Reset, Source, Scratch, Menu arrows, Talkover, Balance.
 *
 * nameMatch covers DJ Console RMX (factory USB / chart name) and "Advanced"
 * (Mixxx mapping name on the same hardware as catalog id
 * hercules-dj-console-rmx). RMX2 is excluded.
 */

import type {
	ControlHint,
	DeviceMap,
	DeviceMapEntry,
	MidiBinding,
	MidiSource
} from '$lib/rb/midi/midi-types';

/** Factory default: sent table status Bx with x = 0. */
const CH = 1;
const PADS = [1, 2, 3, 4, 5, 6] as const;
type Pad = (typeof PADS)[number];
type Deck = 1 | 2;

const cc = (id: number): MidiSource => ({ ch: CH, kind: 'cc', id });

function _button(id: number, action: MidiBinding['action']): MidiBinding {
	return { source: cc(id), action, ccButton: true };
}

function _keypads(deck: Deck, firstCc: number): MidiBinding[] {
	return PADS.map((pad: Pad) =>
		_button(firstCc + pad - 1, { type: 'controller_pad', deck, pad, shifted: false })
	);
}

function _mixerStrip(
	deck: Deck,
	ids: { volume: number; gain: number; treble: number; medium: number; bass: number; pitch: number }
): MidiBinding[] {
	return [
		// [MAN] Analog – Coarse, 00 > 7F: Full Down > Full Up (volume / pitch).
		{ source: cc(ids.volume), action: { type: 'mixer_channel', deck, target: 'fader' } },
		{ source: cc(ids.gain), action: { type: 'mixer_channel', deck, target: 'trim' } },
		{
			source: cc(ids.treble),
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'high' }
		},
		{
			source: cc(ids.medium),
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'mid' }
		},
		{ source: cc(ids.bass), action: { type: 'mixer_channel', deck, target: 'eq', band: 'low' } },
		{ source: cc(ids.pitch), action: { type: 'deck_pitch', deck, lsbOffset: null } }
	];
}

const BINDINGS: MidiBinding[] = [
	// [MAN] p.24 Left Keypad 1..6: CC 0x01..0x06. FX/pad surface; no FX-named chart row.
	..._keypads(1, 0x01),
	// [MAN] p.24 Left Sync 0x07, Play/Pause 0x0B, Cue 0x0C.
	_button(0x07, { type: 'deck_sync_toggle', deck: 1 }),
	_button(0x0b, { type: 'deck_play_toggle', deck: 1 }),
	_button(0x0c, { type: 'deck_cue', deck: 1 }),
	// [MAN] p.24 Left Load 0x12, Left Headphone Cue 0x14.
	_button(0x12, { type: 'browse_load', deck: 1 }),
	_button(0x14, { type: 'channel_cue', deck: 1 }),
	// [MAN] p.24 Right Load 0x16, Right Headphone Cue 0x18.
	_button(0x16, { type: 'browse_load', deck: 2 }),
	_button(0x18, { type: 'channel_cue', deck: 2 }),
	// [MAN] p.24 Right Keypad 1..6: CC 0x19..0x1E.
	..._keypads(2, 0x19),
	// [MAN] p.24 Right Sync 0x1F, Play/Pause 0x23, Cue 0x24.
	_button(0x1f, { type: 'deck_sync_toggle', deck: 2 }),
	_button(0x23, { type: 'deck_play_toggle', deck: 2 }),
	_button(0x24, { type: 'deck_cue', deck: 2 }),
	// [MAN] p.25 Left/Right Jog Wheel 0x2F/0x30: Incremental – Coarse two's complement.
	{ source: cc(0x2f), action: { type: 'deck_jog', deck: 1 }, relative: true },
	{ source: cc(0x30), action: { type: 'deck_jog', deck: 2 }, relative: true },
	// [MAN] p.25 Left Pitch 0x31, Volume 0x32, Gain 0x33, Treble 0x34, Medium 0x35, Bass 0x36.
	..._mixerStrip(1, {
		pitch: 0x31,
		volume: 0x32,
		gain: 0x33,
		treble: 0x34,
		medium: 0x35,
		bass: 0x36
	}),
	// [MAN] p.25 Main Volume 0x38, Cross Fader 0x39, Cue Mix 0x3A.
	{ source: cc(0x38), action: { type: 'mixer_global', target: 'master' } },
	{ source: cc(0x39), action: { type: 'mixer_global', target: 'crossfader' } },
	{ source: cc(0x3a), action: { type: 'headphone_mix' } },
	// [MAN] p.25 Right Pitch 0x3B, Volume 0x3C, Gain 0x3D, Treble 0x3E, Medium 0x3F, Bass 0x40.
	..._mixerStrip(2, {
		pitch: 0x3b,
		volume: 0x3c,
		gain: 0x3d,
		treble: 0x3e,
		medium: 0x3f,
		bass: 0x40
	})
];

const HINTS: ControlHint[] = [
	// [MAN] p.24 remaining sent-table buttons; no matching MidiAction.
	{ source: cc(0x08), label: 'Beatlock (deck 1)' },
	{ source: cc(0x09), label: 'Previous (deck 1)' },
	{ source: cc(0x0a), label: 'Next (deck 1)' },
	{ source: cc(0x0d), label: 'Stop (deck 1)' },
	{ source: cc(0x0e), label: 'Kill Treble (deck 1)' },
	{ source: cc(0x0f), label: 'Kill Medium (deck 1)' },
	{ source: cc(0x10), label: 'Kill Bass (deck 1)' },
	{ source: cc(0x11), label: 'Reset (deck 1)' },
	{ source: cc(0x13), label: 'Source (deck 1)' },
	{ source: cc(0x15), label: 'Beatlock (deck 2)' },
	{ source: cc(0x17), label: 'Source (deck 2)' },
	{ source: cc(0x20), label: 'Reset (deck 2)' },
	{ source: cc(0x21), label: 'Previous (deck 2)' },
	{ source: cc(0x22), label: 'Next (deck 2)' },
	{ source: cc(0x25), label: 'Stop (deck 2)' },
	{ source: cc(0x26), label: 'Kill Treble (deck 2)' },
	{ source: cc(0x27), label: 'Kill Medium (deck 2)' },
	{ source: cc(0x28), label: 'Kill Bass (deck 2)' },
	{ source: cc(0x29), label: 'Scratch' },
	// [MAN] p.25 Menu arrows are buttons, not a relative encoder (no browse_up).
	{ source: cc(0x2a), label: 'Menu Up' },
	{ source: cc(0x2b), label: 'Menu Down' },
	{ source: cc(0x2c), label: 'Menu Left' },
	{ source: cc(0x2d), label: 'Menu Right' },
	{ source: cc(0x2e), label: 'Talkover' },
	{ source: cc(0x37), label: 'Balance' }
];

/** Matches DJ Console RMX (and "Advanced" on the same hardware) and excludes RMX2.
 * Written without lookaround: the Rust engine compiles nameMatch with regex_lite. */
export const HERCULES_DJ_CONSOLE_RMX_ADVANCED_MAP: DeviceMap = {
	vendor: 'Hercules',
	nameMatch: 'DJ Console RMX(?:$|[^2])',
	bindings: BINDINGS,
	hints: HINTS
};

/** Self-registration: maps/entries.ts discovers this export, so no shared list names this map. */
export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{
		key: 'HERCULES_DJ_CONSOLE_RMX_ADVANCED_MAP',
		map: HERCULES_DJ_CONSOLE_RMX_ADVANCED_MAP,
		live: true
	}
];
