/**
 * DJ TechTools Midi Fighter Twister device map (issue #10815).
 *
 * SOURCE OF TRUTH: DJ TechTools, "Midi Fighter Twister User Guide", Ver 2026.06.01
 * (29 pp), https://drive.google.com/file/d/0B-QvIds_FsH3Z0ZLT041VnZfOTA/view ,
 * linked as "User Guide" from
 * https://store.djtechtools.com/pages/midi-fighter-setup/midi-fighter-twister .
 * Sections: p.16 "Encoder & Encoder Switch MIDI Number & Channel"; Appendix 1
 * "Bank 1 MIDI" (p.17). The PDF is not redistributed.
 *
 * Appendix 1 encoder columns are 0-based in the guide; this file uses 1..16
 * `MidiSource.ch`. Encoder 1 = ch 1 CC 0; switch 1 = ch 2 CC 0.
 *
 * Bank 1 grid (encoder n = CC n-1 on ch 1):
 *   row 1: enc1 D1 EQ high, enc2 D1 trim, enc3 D2 trim, enc4 D2 EQ high
 *   row 2: enc5 D1 EQ mid, enc6 D1 filter, enc7 D2 filter, enc8 D2 EQ mid
 *   row 3: enc9 D1 EQ low, enc10 crossfader, enc11 browse, enc12 D2 EQ low
 *   row 4: enc13 D1 fader, enc14 D1 jog, enc15 D2 jog, enc16 D2 fader
 *   ([Guide] p.17 Bank 1: encoders ch 1 CC 0-15, switches ch 2 CC 0-15)
 *
 * REQUIRED DEVICE CONFIGURATION (Midi Fighter Utility): encoders 11, 14 and 15
 * MIDI type `Enc 3FH/41H`; every other bank-1 encoder MIDI type `CC`; encoder
 * switches CC (guide default) in momentary/hold mode. The guide does not
 * document factory encoder types per encoder; this is user configuration.
 *
 * FX: no dedicated FX section on the Twister; colour FX is per-deck filter
 * (`mixer_channel` `filter`, encoders 6 and 7). Open DJ has no beat-FX
 * MidiAction (midi-types.ts), so nothing binds beat FX.
 *
 * Source conflicts: side switches on System ch 4 (guide p.7/p.12; third-party
 * ch 3 notes rejected). Firmware mentions eight banks; Appendix 1 lists four.
 * Ch 4 side CC 8-31 overlap bank-2+ switch CC ranges on ch 2; channels differ.
 *
 * Port name: no manufacturer USB/WebMIDI string in the guide; community reports
 * `Midi Fighter Twister` (optional numeric suffix). nameMatch is unanchored.
 *
 * Banks 2-4, shift encoders (ch 5), bank-select and sequencer are unbound.
 *
 * NOT HARDWARE CONFIRMED.
 *
 * No LED rules: ring is white-only; RGB is on encoder switches (ch 2) and is
 * not hardware-checked for feedback rules.
 */

import type { ControlHint, DeviceMap, DeviceMapEntry, MidiBinding } from '$lib/rb/midi/midi-types';

function enc(cc: number): MidiBinding['source'] {
	return { ch: 1, kind: 'cc', id: cc };
}

function sw(cc: number): MidiBinding['source'] {
	return { ch: 2, kind: 'cc', id: cc };
}

const BINDINGS: MidiBinding[] = [
	// ch 1 encoders [Guide] p.17 Bank 1
	{
		source: enc(0),
		action: { type: 'mixer_channel', deck: 1, target: 'eq', band: 'high' }
	},
	{
		source: enc(1),
		action: { type: 'mixer_channel', deck: 1, target: 'trim' }
	},
	{
		source: enc(2),
		action: { type: 'mixer_channel', deck: 2, target: 'trim' }
	},
	{
		source: enc(3),
		action: { type: 'mixer_channel', deck: 2, target: 'eq', band: 'high' }
	},
	{
		source: enc(4),
		action: { type: 'mixer_channel', deck: 1, target: 'eq', band: 'mid' }
	},
	{
		source: enc(5),
		action: { type: 'mixer_channel', deck: 1, target: 'filter' }
	},
	{
		source: enc(6),
		action: { type: 'mixer_channel', deck: 2, target: 'filter' }
	},
	{
		source: enc(7),
		action: { type: 'mixer_channel', deck: 2, target: 'eq', band: 'mid' }
	},
	{
		source: enc(8),
		action: { type: 'mixer_channel', deck: 1, target: 'eq', band: 'low' }
	},
	{ source: enc(9), action: { type: 'mixer_global', target: 'crossfader' } },
	{
		source: enc(10),
		action: { type: 'browse_encoder' },
		relative: true,
		relativeCentered: true,
		relativeUnit: true
	},
	{
		source: enc(11),
		action: { type: 'mixer_channel', deck: 2, target: 'eq', band: 'low' }
	},
	{
		source: enc(12),
		action: { type: 'mixer_channel', deck: 1, target: 'fader' }
	},
	{
		source: enc(13),
		action: { type: 'deck_jog', deck: 1 },
		relative: true,
		relativeCentered: true
	},
	{
		source: enc(14),
		action: { type: 'deck_jog', deck: 2 },
		relative: true,
		relativeCentered: true
	},
	{
		source: enc(15),
		action: { type: 'mixer_channel', deck: 2, target: 'fader' }
	},
	// ch 2 encoder switches [Guide] p.17 Bank 1
	{
		source: sw(1),
		action: { type: 'deck_sync_toggle', deck: 1 },
		ccButton: true
	},
	{
		source: sw(2),
		action: { type: 'deck_sync_toggle', deck: 2 },
		ccButton: true
	},
	{ source: sw(5), action: { type: 'deck_cue', deck: 1 }, ccButton: true },
	{ source: sw(6), action: { type: 'deck_cue', deck: 2 }, ccButton: true },
	{ source: sw(9), action: { type: 'browse_load', deck: 1 }, ccButton: true },
	{ source: sw(10), action: { type: 'browse_load', deck: 2 }, ccButton: true },
	{ source: sw(12), action: { type: 'channel_cue', deck: 1 }, ccButton: true },
	{ source: sw(15), action: { type: 'channel_cue', deck: 2 }, ccButton: true },
	{ source: sw(13), action: { type: 'deck_play_toggle', deck: 1 }, ccButton: true },
	{ source: sw(14), action: { type: 'deck_play_toggle', deck: 2 }, ccButton: true }
];

const HINTS: ControlHint[] = [
	{ source: sw(0), label: 'Encoder 1 switch (bank 1)' },
	{ source: sw(3), label: 'Encoder 4 switch (bank 1)' },
	{ source: sw(4), label: 'Encoder 5 switch (bank 1)' },
	{ source: sw(7), label: 'Encoder 8 switch (bank 1)' },
	{ source: sw(8), label: 'Encoder 9 switch (bank 1)' },
	{ source: sw(11), label: 'Encoder 12 switch (bank 1)' },
	{ source: { ch: 4, kind: 'cc', id: 8 }, label: 'LH side switch 1 (bank 1)' },
	{ source: { ch: 4, kind: 'cc', id: 9 }, label: 'LH side switch 2 (bank 1)' },
	{ source: { ch: 4, kind: 'cc', id: 10 }, label: 'LH side switch 3 (bank 1)' },
	{ source: { ch: 4, kind: 'cc', id: 11 }, label: 'RH side switch 1 (bank 1)' },
	{ source: { ch: 4, kind: 'cc', id: 12 }, label: 'RH side switch 2 (bank 1)' },
	{ source: { ch: 4, kind: 'cc', id: 13 }, label: 'RH side switch 3 (bank 1)' }
];

export const MIDI_FIGHTER_TWISTER_MAP: DeviceMap = {
	vendor: 'DJ TechTools',
	nameMatch: 'Midi Fighter Twister',
	bindings: BINDINGS,
	hints: HINTS
};

/** Self-registration: maps/entries.ts discovers this export, so no shared list names this map. */
export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{ key: 'MIDI_FIGHTER_TWISTER_MAP', map: MIDI_FIGHTER_TWISTER_MAP, live: true }
];
