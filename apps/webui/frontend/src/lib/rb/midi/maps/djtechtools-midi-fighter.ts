/**
 * DJ TechTools Midifighter (original kit-set, 2009-2010) P0 device map.
 *
 * SOURCES (every wire number below is cited, none invented):
 *   [MF] README.txt, "Features of the Midifighter Firmware v1.2" >
 *        "MIDI Implementation" and "Expansion Ports":
 *        https://sourceforge.net/p/midifighter/code/HEAD/tree/README.txt?format=raw
 *   [USB] usb_descriptors.c, ProductString / ManufacturerString:
 *        https://sourceforge.net/p/midifighter/code/HEAD/tree/usb_descriptors.c?format=raw
 *   [EE] eeprom.c, eeprom_factory_reset() factory defaults:
 *        https://sourceforge.net/p/midifighter/code/HEAD/tree/eeprom.c?format=raw
 *   [FW] midifighter.c, analog-port comment and midi_stream_cc(16 + i, value):
 *        https://sourceforge.net/p/midifighter/code/HEAD/tree/midifighter.c?format=raw
 *
 * hardware_confirmed: false - no unit was plugged in; every wire is theory from [MF].
 * Clean room: GPL firmware and Mixxx were read for numbers only, no code copied.
 * Scope: factory-default Normal mode (channel 3, base note 36, Four Banks off).
 *   The device has no jog wheel, encoder, built-in fader or FX control; mixer
 *   bindings use the optional expansion analog inputs CC 16-19 (off at factory).
 *
 * Requirements (mini-PRD):
 *   ✔︎ 2-deck transport, sync, auto-loop, hot cues A/B, load, PFL from the 4x4 grid.
 *     [if] a key listed in [MF] within P0 scope has no binding here [then ⛔️]
 *     [if] a binding carries a number absent from [MF] [then ⛔️] fabricated wire
 *   ✔︎ Expansion analog CC 16-19: channel faders, crossfader, master (OUR assignment).
 *   ✔︎ Key LED feedback via LedRule (Note On velocity 0x7F on / 0x00 off per [MF]).
 *   → Jog, browse encoder, beat/colour FX: not on device, not in chart, unbound.
 */

import type {
	ControlHint,
	DeviceMap,
	DeviceMapEntry,
	LedRule,
	MidiBinding
} from '$lib/rb/midi/midi-types';

const CH = 3;

// --------------------------------------------------------------- bindings

/** Per-deck grid keys. Deck 1 = left two columns, deck 2 = right two. [MF] grid. */
function _deckBindings(deck: 1 | 2): MidiBinding[] {
	if (deck === 1) {
		return [
			// row 1 (bottom)
			// [MF] note 0x24 (36) bottom-left
			{ source: { ch: CH, kind: 'note', id: 36 }, action: { type: 'deck_cue', deck: 1 } },
			// [MF] note 0x25 (37)
			{
				source: { ch: CH, kind: 'note', id: 37 },
				action: { type: 'deck_play_toggle', deck: 1 }
			},
			// row 2
			// [MF] note 0x28 (40)
			{
				source: { ch: CH, kind: 'note', id: 40 },
				action: { type: 'deck_sync_toggle', deck: 1 }
			},
			// [MF] note 0x29 (41)
			{
				source: { ch: CH, kind: 'note', id: 41 },
				action: { type: 'deck_auto_loop_toggle', deck: 1 }
			},
			// row 3
			// [MF] note 0x2C (44)
			{
				source: { ch: CH, kind: 'note', id: 44 },
				action: { type: 'deck_hot_cue', deck: 1, slot: 'A' }
			},
			// [MF] note 0x2D (45)
			{
				source: { ch: CH, kind: 'note', id: 45 },
				action: { type: 'deck_hot_cue', deck: 1, slot: 'B' }
			},
			// row 4 (top)
			// [MF] note 0x30 (48)
			{ source: { ch: CH, kind: 'note', id: 48 }, action: { type: 'browse_load', deck: 1 } },
			// [MF] note 0x31 (49)
			{ source: { ch: CH, kind: 'note', id: 49 }, action: { type: 'channel_cue', deck: 1 } }
		];
	}
	return [
		// row 1 (bottom)
		// [MF] note 0x26 (38)
		{ source: { ch: CH, kind: 'note', id: 38 }, action: { type: 'deck_cue', deck: 2 } },
		// [MF] note 0x27 (39)
		{
			source: { ch: CH, kind: 'note', id: 39 },
			action: { type: 'deck_play_toggle', deck: 2 }
		},
		// row 2
		// [MF] note 0x2A (42)
		{
			source: { ch: CH, kind: 'note', id: 42 },
			action: { type: 'deck_sync_toggle', deck: 2 }
		},
		// [MF] note 0x2B (43)
		{
			source: { ch: CH, kind: 'note', id: 43 },
			action: { type: 'deck_auto_loop_toggle', deck: 2 }
		},
		// row 3
		// [MF] note 0x2E (46)
		{
			source: { ch: CH, kind: 'note', id: 46 },
			action: { type: 'deck_hot_cue', deck: 2, slot: 'A' }
		},
		// [MF] note 0x2F (47)
		{
			source: { ch: CH, kind: 'note', id: 47 },
			action: { type: 'deck_hot_cue', deck: 2, slot: 'B' }
		},
		// row 4 (top)
		// [MF] note 0x32 (50)
		{ source: { ch: CH, kind: 'note', id: 50 }, action: { type: 'browse_load', deck: 2 } },
		// [MF] note 0x33 (51)
		{ source: { ch: CH, kind: 'note', id: 51 }, action: { type: 'channel_cue', deck: 2 } }
	];
}

/** Expansion analog inputs. OUR assignment for user-wired DIY faders; [MF] names the
 * wire (Analog A1..A4, full range CC 16..19), not the mixer function. */
const _EXPANSION_BINDINGS: MidiBinding[] = [
	// [MF] Analog A1, CC 0x10 (16), full range
	{
		source: { ch: CH, kind: 'cc', id: 16 },
		action: { type: 'mixer_channel', deck: 1, target: 'fader' }
	},
	// [MF] Analog A2, CC 0x11 (17), full range
	{
		source: { ch: CH, kind: 'cc', id: 17 },
		action: { type: 'mixer_channel', deck: 2, target: 'fader' }
	},
	// [MF] Analog A3, CC 0x12 (18), full range
	{
		source: { ch: CH, kind: 'cc', id: 18 },
		action: { type: 'mixer_global', target: 'crossfader' }
	},
	// [MF] Analog A4, CC 0x13 (19), full range
	{
		source: { ch: CH, kind: 'cc', id: 19 },
		action: { type: 'mixer_global', target: 'master' }
	}
];

// -------------------------------------------------------- best-guess hints

/** Documented expansion ports not bound ([MF] / [FW], factory-off until wired). */
const _EXPANSION_HINTS: ControlHint[] = [
	// [MF] Expansion Ports, basenote+16..+19 in Normal mode
	{ source: { ch: CH, kind: 'note', id: 52 }, label: 'Expansion digital input D1' },
	{ source: { ch: CH, kind: 'note', id: 53 }, label: 'Expansion digital input D2' },
	{ source: { ch: CH, kind: 'note', id: 54 }, label: 'Expansion digital input D3' },
	{ source: { ch: CH, kind: 'note', id: 55 }, label: 'Expansion digital input D4' },
	// [MF]/[FW] upper-half smart-fader notes on CC 20..23
	{ source: { ch: CH, kind: 'cc', id: 20 }, label: 'Expansion analog A1 upper half' },
	{ source: { ch: CH, kind: 'cc', id: 21 }, label: 'Expansion analog A2 upper half' },
	{ source: { ch: CH, kind: 'cc', id: 22 }, label: 'Expansion analog A3 upper half' },
	{ source: { ch: CH, kind: 'cc', id: 23 }, label: 'Expansion analog A4 upper half' }
];

// ------------------------------------------------------------ led feedback

/** Key LEDs: [MF] "activated by sending a NoteOn ... same MIDI channel", velocity = state. */
function _deckLeds(deck: 1 | 2): LedRule[] {
	if (deck === 1) {
		return [
			{
				trigger: { kind: 'deck_playing', deck: 1 },
				out: { ch: CH, note: 37, velocityOn: 0x7f, velocityOff: 0x00 }
			},
			{
				trigger: { kind: 'beat_sync_enabled', deck: 1 },
				out: { ch: CH, note: 40, velocityOn: 0x7f, velocityOff: 0x00 }
			},
			{
				trigger: { kind: 'loop_engaged', deck: 1 },
				out: { ch: CH, note: 41, velocityOn: 0x7f, velocityOff: 0x00 }
			},
			{
				trigger: { kind: 'hot_cue_present', deck: 1, slot: 'A' },
				out: { ch: CH, note: 44, velocityOn: 0x7f, velocityOff: 0x00 }
			},
			{
				trigger: { kind: 'hot_cue_present', deck: 1, slot: 'B' },
				out: { ch: CH, note: 45, velocityOn: 0x7f, velocityOff: 0x00 }
			},
			{
				trigger: { kind: 'deck_loaded', deck: 1 },
				out: { ch: CH, note: 48, velocityOn: 0x7f, velocityOff: 0x00 }
			},
			{
				trigger: { kind: 'channel_cue_enabled', deck: 1 },
				out: { ch: CH, note: 49, velocityOn: 0x7f, velocityOff: 0x00 }
			}
		];
	}
	return [
		{
			trigger: { kind: 'deck_playing', deck: 2 },
			out: { ch: CH, note: 39, velocityOn: 0x7f, velocityOff: 0x00 }
		},
		{
			trigger: { kind: 'beat_sync_enabled', deck: 2 },
			out: { ch: CH, note: 42, velocityOn: 0x7f, velocityOff: 0x00 }
		},
		{
			trigger: { kind: 'loop_engaged', deck: 2 },
			out: { ch: CH, note: 43, velocityOn: 0x7f, velocityOff: 0x00 }
		},
		{
			trigger: { kind: 'hot_cue_present', deck: 2, slot: 'A' },
			out: { ch: CH, note: 46, velocityOn: 0x7f, velocityOff: 0x00 }
		},
		{
			trigger: { kind: 'hot_cue_present', deck: 2, slot: 'B' },
			out: { ch: CH, note: 47, velocityOn: 0x7f, velocityOff: 0x00 }
		},
		{
			trigger: { kind: 'deck_loaded', deck: 2 },
			out: { ch: CH, note: 50, velocityOn: 0x7f, velocityOff: 0x00 }
		},
		{
			trigger: { kind: 'channel_cue_enabled', deck: 2 },
			out: { ch: CH, note: 51, velocityOn: 0x7f, velocityOff: 0x00 }
		}
	];
}

// -------------------------------------------------------------- device map

export const DJTT_MIDI_FIGHTER_MAP: DeviceMap = {
	vendor: 'DJ TechTools',
	// [USB] ProductString L"Midifighter". Later Midi Fighter 3D / Twister / Spectra / 64
	// enumerate with a space ("Midi Fighter ...") and different wires, so they must not match.
	nameMatch: 'Midifighter',
	bindings: [..._deckBindings(1), ..._deckBindings(2), ..._EXPANSION_BINDINGS],
	leds: [..._deckLeds(1), ..._deckLeds(2)],
	hints: _EXPANSION_HINTS
};

/** Self-registration: maps/entries.ts discovers this export, so no shared list names this map. */
export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{ key: 'DJTT_MIDI_FIGHTER_MAP', map: DJTT_MIDI_FIGHTER_MAP, live: true }
];
