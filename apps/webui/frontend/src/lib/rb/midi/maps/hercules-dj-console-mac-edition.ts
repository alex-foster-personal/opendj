/**
 * Hercules DJ Console Mac Edition live P0 device map (issue #10832).
 *
 * SOURCE OF TRUTH for every Note/CC number in this file: Hercules
 * "DJ Console Mk2 Mac Edition" user manual, section 7 "MIDI STANDARD
 * SUPPORT" (manual pages 18/24–20/24), MIDI input table then MIDI output
 * table:
 *   https://ts.hercules.com/download/sound/manuals/Mk2/Traktor/DJ_Console_Mk2_MAC_eng.pdf
 * Hercules published no Mac-Edition-titled MIDI list; this is the published
 * Hercules MIDI table for the Mac DJ Console family. Cited as [PDF] §7.
 * Do not redistribute the PDF.
 *
 * USB / Audio MIDI Setup name (Hercules FAQ
 * https://ts.hercules.com/faqs/eng/her_eng_00300.pdf): "Hercules DJ Console".
 * Catalog model: "Hercules DJ Console Mac Edition". nameMatch hits both, plus
 * "Port" / "MIDI" suffixes, and excludes Mk2 / Mk4 / 4-Mx / RMX without
 * lookaround (regex_lite).
 *
 * Channel: the sheet prints status B0 (Control Change on MIDI channel 1),
 * not Bx. Every binding uses ch: 1.
 *
 * Transport / FX / headphone rows are CC buttons (7F pressed / 00 released)
 * so they bind with ccButton: true. Jogs and pitch knobs are "Incremental –
 * Coarse" (7F..40 CCW Slow>Fast / 01..3F CW Slow>Fast around 64): jogs bind
 * relative + relativeCentered. Pitch knobs use the same encoding but there
 * is no relative deck_pitch contract, so they are hints.
 *
 * LEDs on the sheet are CC output (B0 nn). LedRule.out is Note On only, so
 * this map has no leds array.
 *
 * FX: the sheet has FX Select and FX 1/2/3 per deck. MidiAction has no
 * beat-FX / colour-FX case (midi-types.ts P0 fence). This device has no
 * filter / colour-FX knob. FX buttons are hints, same as Pioneer Beat FX.
 *
 * Mixxx's Mac Edition ingest is GPL and is not a source. It uses channel 2
 * and analog CC ids (crossfader 40, volumes 43/44, jogs 45/46) that are
 * absent from this chart; those ids stay unbound. On this sheet 0x28 is
 * ministick X and the crossfader is 0x31.
 *
 * NOT HARDWARE CONFIRMED.
 */

import type {
	ControlHint,
	DeviceMap,
	DeviceMapEntry,
	MidiAction,
	MidiBinding
} from '$lib/rb/midi/midi-types';

/** Sheet status B0 = MIDI channel 1. */
const CH = 1;

/** FAQ USB name, catalog "Mac Edition", Audio MIDI Setup Port/MIDI suffixes.
 * Mk2 / Mk4 / 4-Mx / RMX fail the trailing Port|MIDI|$ alternative. */
const _NAME_MATCH = 'Hercules\\s+DJ\\s+Console(?:\\s+Mac\\s+Edition)?(?:$|\\s+Port|\\s+MIDI)';

function _ccButton(id: number, action: MidiAction): MidiBinding {
	return { source: { ch: CH, kind: 'cc', id }, action, ccButton: true };
}

function _cc(id: number, action: MidiAction, extra?: Partial<MidiBinding>): MidiBinding {
	return { source: { ch: CH, kind: 'cc', id }, action, ...extra };
}

function _hint(id: number, label: string): ControlHint {
	return { source: { ch: CH, kind: 'cc', id }, label };
}

const _BINDINGS: MidiBinding[] = [
	// [PDF] §7 Left Play/Pause B0 08
	_ccButton(0x08, { type: 'deck_play_toggle', deck: 1 }),
	// [PDF] §7 Left Cue B0 09
	_ccButton(0x09, { type: 'deck_cue', deck: 1 }),
	// [PDF] §7 Left AutoBeat Select B0 0A
	_ccButton(0x0a, { type: 'deck_sync_toggle', deck: 1 }),
	// [PDF] §7 Left Pitch Bend "+" B0 13
	_ccButton(0x13, { type: 'deck_tempo_nudge', deck: 1, direction: 1 }),
	// [PDF] §7 Left Pitch Bend "-" B0 14
	_ccButton(0x14, { type: 'deck_tempo_nudge', deck: 1, direction: -1 }),
	// [PDF] §7 Left Master Tempo B0 16
	_ccButton(0x16, { type: 'deck_key_sync_toggle', deck: 1 }),
	// [PDF] §7 Headphone Deck A B0 21
	_ccButton(0x21, { type: 'channel_cue', deck: 1 }),

	// [PDF] §7 Right Play/Pause B0 02
	_ccButton(0x02, { type: 'deck_play_toggle', deck: 2 }),
	// [PDF] §7 Right Cue B0 03
	_ccButton(0x03, { type: 'deck_cue', deck: 2 }),
	// [PDF] §7 Right AutoBeat Select B0 04
	_ccButton(0x04, { type: 'deck_sync_toggle', deck: 2 }),
	// [PDF] §7 Right Pitch Bend "+" B0 17
	_ccButton(0x17, { type: 'deck_tempo_nudge', deck: 2, direction: 1 }),
	// [PDF] §7 Right Pitch Bend "-" B0 18
	_ccButton(0x18, { type: 'deck_tempo_nudge', deck: 2, direction: -1 }),
	// [PDF] §7 Right Master Tempo B0 1A
	_ccButton(0x1a, { type: 'deck_key_sync_toggle', deck: 2 }),
	// [PDF] §7 Headphone Deck B B0 22
	_ccButton(0x22, { type: 'channel_cue', deck: 2 }),

	// [PDF] §7 Cross Fader B0 31 (00 left → 7F right)
	_cc(0x31, { type: 'mixer_global', target: 'crossfader' }),
	// [PDF] §7 Left Volume Slider B0 32
	_cc(0x32, { type: 'mixer_channel', deck: 1, target: 'fader' }),
	// [PDF] §7 Right Volume Slider B0 33
	_cc(0x33, { type: 'mixer_channel', deck: 2, target: 'fader' }),
	// [PDF] §7 Left Bass / Medium / Treble B0 2E / 2F / 30
	_cc(0x2e, { type: 'mixer_channel', deck: 1, target: 'eq', band: 'low' }),
	_cc(0x2f, { type: 'mixer_channel', deck: 1, target: 'eq', band: 'mid' }),
	_cc(0x30, { type: 'mixer_channel', deck: 1, target: 'eq', band: 'high' }),
	// [PDF] §7 Right Bass / Medium / Treble B0 2B / 2C / 2D
	_cc(0x2b, { type: 'mixer_channel', deck: 2, target: 'eq', band: 'low' }),
	_cc(0x2c, { type: 'mixer_channel', deck: 2, target: 'eq', band: 'mid' }),
	_cc(0x2d, { type: 'mixer_channel', deck: 2, target: 'eq', band: 'high' }),
	// [PDF] §7 Left Jog Wheel B0 36 Incremental-Coarse around 64
	_cc(0x36, { type: 'deck_jog', deck: 1 }, { relative: true, relativeCentered: true }),
	// [PDF] §7 Right Jog Wheel B0 37 Incremental-Coarse around 64
	_cc(0x37, { type: 'deck_jog', deck: 2 }, { relative: true, relativeCentered: true })
];

/** Cited input rows with no honest MidiAction. Labels are the sheet names. */
const _HINTS: ControlHint[] = [
	// [PDF] §7 Left / Right Prev. Track B0 0B / 05 — deck skip, not a browse encoder
	_hint(0x0b, 'Prev Track (deck 1)'),
	_hint(0x05, 'Prev Track (deck 2)'),
	// [PDF] §7 Left / Right Next Track B0 0C / 06
	_hint(0x0c, 'Next Track (deck 1)'),
	_hint(0x06, 'Next Track (deck 2)'),
	// [PDF] §7 Left / Right FX Select B0 07 / 01 — no beat-FX MidiAction
	_hint(0x07, 'FX Select (deck 1)'),
	_hint(0x01, 'FX Select (deck 2)'),
	// [PDF] §7 Left FX "1"/"2"/"3" B0 0F / 0E / 0D
	_hint(0x0f, 'FX 1 (deck 1)'),
	_hint(0x0e, 'FX 2 (deck 1)'),
	_hint(0x0d, 'FX 3 (deck 1)'),
	// [PDF] §7 Right FX "1"/"2"/"3" B0 10 / 11 / 12
	_hint(0x10, 'FX 1 (deck 2)'),
	_hint(0x11, 'FX 2 (deck 2)'),
	_hint(0x12, 'FX 3 (deck 2)'),
	// [PDF] §7 Left / Right Pitch Knob B0 34 / 35 (speed-encoded; not a 7-bit fader)
	_hint(0x34, 'Pitch knob (deck 1)'),
	_hint(0x35, 'Pitch knob (deck 2)'),
	// [PDF] §7 Left / Right Mouse Button B0 1B / 1C
	_hint(0x1b, 'Mouse button (deck 1)'),
	_hint(0x1c, 'Mouse button (deck 2)'),
	// [PDF] §7 Left / Right Deck Audio Source Select B0 1D / 1E
	_hint(0x1d, 'Deck audio source (deck 1)'),
	_hint(0x1e, 'Deck audio source (deck 2)'),
	// [PDF] §7 Headphone Split / Mix B0 23 / 24 (mode buttons, not headphone_mix)
	_hint(0x23, 'Headphone Split'),
	_hint(0x24, 'Headphone Mix'),
	// [PDF] §7 Ministick X / Y B0 28 / 39 — 0x28 is NOT the crossfader
	_hint(0x28, 'Ministick X'),
	_hint(0x39, 'Ministick Y')
];

/** [PDF] §7 MIDI output: Update Analog Controls B0 7F, 7F = update. */
const _INIT: number[][] = [[0xb0, 0x7f, 0x7f]];

export const HERCULES_DJ_CONSOLE_MAC_EDITION_MAP: DeviceMap = {
	vendor: 'Hercules',
	nameMatch: _NAME_MATCH,
	bindings: _BINDINGS,
	hints: _HINTS,
	init: _INIT
};

/** Self-registration: maps/entries.ts discovers this export, so no shared list names this map. */
export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{
		key: 'HERCULES_DJ_CONSOLE_MAC_EDITION_MAP',
		map: HERCULES_DJ_CONSOLE_MAC_EDITION_MAP,
		live: true
	}
];
