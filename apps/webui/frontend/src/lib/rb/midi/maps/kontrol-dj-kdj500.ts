/**
 * Kontrol DJ KDJ500 P0 device map (issue #10850, SPRINT400-A21).
 *
 * SOURCES (every wire below is cited, none invented):
 *   [MAN] KDJ500 user manual.pdf (c) 2006 Kontrol Dj, legacy downloads folder
 *        https://drive.google.com/folderview?id=0B09d9nFit33wdUpIZk5OZ0wxRTA
 *        Ch.6 quick user guide (SHIFT, SELECT banks, JOG MODE); ch.7 Konverter
 *        USB-to-MIDI bridge; ch.8-14 app port "Kontrol Dj Midi In: Port 1";
 *        ch.15 Konverter template programming (90/B0 on channel 1).
 *   [T3] Konverter template "Traktor Dj v3 (FX)" from Application Konverter
 *        for Windows.zip (file id 0B09d9nFit33weTV2N1d6TmZ1Qmc):
 *        Users/Traktor 3/kdj_traktor3_template.xml (per-control list) and
 *        kdj_traktor3_template.pdf (overlay chart).
 *
 * hardware_confirmed: false (no unit on the bench; wires taken only from [MAN]/[T3]).
 * NOT HARDWARE CONFIRMED.
 *
 * Template: wires assume Konverter with "Traktor Dj v3 (FX)" loaded. Other
 * templates (VirtualDj, MixVibes, Traktor 2) use different numbers.
 *
 * Clean room: the GPL Mixxx mapping was used only to cross-check numbers already
 * taken from [T3]. tools/deck-diagrams/devices/kontrol-dj-kdj500/midi.json is
 * not a source.
 *
 * Requirements (mini-PRD):
 *   [if] a binding's (channel, kind, id) is absent from [T3] [then] reject in review
 *   [if] play, cue, sync, both faders, crossfader or either jog is unbound [then] map incomplete
 *   -> Track encoder turns are one note per detent (0x66/0x67); browse_encoder needs a
 *      relative CC, so browse scroll is hinted, not bound. LOAD is bound.
 *   -> Jog binds VINYL-mode CC (0x28/0x2C). CD mode (lever off) sends note-per-tick
 *      (0x60/0x61) which deck_jog cannot decode; operator must set JOG MODE to VINYL.
 *   -> No beat-FX MidiAction: FX BUT. 1-4, FX KNOB 2-4 and FX SELECT are hints.
 *      FX KNOB 1 (bank 2) binds mixer_channel filter (colour FX).
 *   -> SHIFT is hardware-encoded (separate shifted numbers); no shift_modifier binding.
 *   -> Konverter is USB-to-MIDI only; no LED rules.
 *   -> Pitch fader direction is unstated in [MAN]/[T3]; left un-inverted, unconfirmed.
 */

import type { ControlHint, DeviceMap, DeviceMapEntry, MidiBinding } from '$lib/rb/midi/midi-types';

const CH = 1;

/** Per-deck wire numbers from [T3] (L deck 1, R deck 2 - not a fixed offset). */
const W = {
	1: {
		play: 0x10,
		cue: 0x0e,
		sync: 0x2d,
		pfl: 0x2c,
		load: 0x14,
		loop: [0x09, 0x0a, 0x0b, 0x0c] as const,
		trim: 0x01,
		eq: [0x02, 0x03, 0x04] as const,
		vol: 0x0b,
		filter: 0x0e,
		pitch: 0x09,
		jog: 0x28
	},
	2: {
		play: 0x26,
		cue: 0x24,
		sync: 0x2f,
		pfl: 0x2e,
		load: 0x28,
		loop: [0x1f, 0x20, 0x21, 0x22] as const,
		trim: 0x05,
		eq: [0x06, 0x07, 0x08] as const,
		vol: 0x0c,
		filter: 0x12,
		pitch: 0x0a,
		jog: 0x2c
	}
} as const;

const LOOP_BEATS = [1, 4, 8, 16] as const;

function _deckBindings(deck: 1 | 2): MidiBinding[] {
	const w = W[deck];
	return [
		// [T3] play (L)/(R) bank 1
		{ source: { ch: CH, kind: 'note', id: w.play }, action: { type: 'deck_play_toggle', deck } },
		// [T3] cue
		{ source: { ch: CH, kind: 'note', id: w.cue }, action: { type: 'deck_cue', deck } },
		// [T3] pfl bank 3 (SHIFT+CUE wire); chart label SYNC
		{ source: { ch: CH, kind: 'note', id: w.sync }, action: { type: 'deck_sync_toggle', deck } },
		// [T3] pfl bank 1 (round CUE by knobs)
		{ source: { ch: CH, kind: 'note', id: w.pfl }, action: { type: 'channel_cue', deck } },
		// [T3] push track (encoder push) -> LOAD
		{ source: { ch: CH, kind: 'note', id: w.load }, action: { type: 'browse_load', deck } },
		...LOOP_BEATS.map((beats, i): MidiBinding => ({
			// [T3] one..four shift layer (chart "1 4 8 16")
			source: { ch: CH, kind: 'note', id: w.loop[i] },
			action: { type: 'deck_beat_loop', deck, beats }
		})),
		// [T3] gain bank 1; T3 overlay labels "PH. VOL."
		{
			source: { ch: CH, kind: 'cc', id: w.trim },
			action: { type: 'mixer_channel', deck, target: 'trim' }
		},
		// [T3] high / mid / low bank 1
		{
			source: { ch: CH, kind: 'cc', id: w.eq[0] },
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'high' }
		},
		{
			source: { ch: CH, kind: 'cc', id: w.eq[1] },
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'mid' }
		},
		{
			source: { ch: CH, kind: 'cc', id: w.eq[2] },
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'low' }
		},
		// [T3] volume fader bank 1
		{
			source: { ch: CH, kind: 'cc', id: w.vol },
			action: { type: 'mixer_channel', deck, target: 'fader' }
		},
		// [T3] gain bank 2 -> FX KNOB 1 (colour FX / filter)
		{
			source: { ch: CH, kind: 'cc', id: w.filter },
			action: { type: 'mixer_channel', deck, target: 'filter' }
		},
		// [T3] pitch fader bank 1 (direction unconfirmed in [MAN]/[T3])
		{
			source: { ch: CH, kind: 'cc', id: w.pitch },
			action: { type: 'deck_pitch', deck, lsbOffset: null }
		},
		// [T3] jog lever on VINYL: right=0x7F, left=0x01. decodeRelative reads 0x7F
		// as -1, so invert makes clockwise mean forward.
		{
			source: { ch: CH, kind: 'cc', id: w.jog },
			action: { type: 'deck_jog', deck },
			relative: true,
			invert: true
		}
	];
}

const _GLOBAL_BINDINGS: MidiBinding[] = [
	// [T3] crossfader bank 1
	{
		source: { ch: CH, kind: 'cc', id: 0x0d },
		action: { type: 'mixer_global', target: 'crossfader' }
	}
];

function _deckHints(deck: 1 | 2): ControlHint[] {
	const d = `(deck ${deck})`;
	const note = (id: number, label: string): ControlHint => ({
		source: { ch: CH, kind: 'note', id },
		label
	});
	const cc = (id: number, label: string): ControlHint => ({
		source: { ch: CH, kind: 'cc', id },
		label
	});

	if (deck === 1) {
		return [
			note(0x00, `Button bank SELECT ${d}`),
			note(0x01, `LOOP / IN ${d}`),
			note(0x02, `LOOP OUT ${d}`),
			note(0x03, `<< MOVE L. ${d}`),
			note(0x04, `MOVE L. >> ${d}`),
			note(0x05, `FX BUT. 1 ${d}`),
			note(0x06, `FX BUT. 2 ${d}`),
			note(0x07, `FX BUT. 3 ${d}`),
			note(0x08, `FX BUT. 4 ${d}`),
			note(0x0d, `SHIFT ${d}`),
			note(0x0f, `CUE > (SHIFT) ${d}`),
			note(0x11, `SET (SHIFT) ${d}`),
			note(0x12, `JOG MODE lever VINYL ${d}`),
			note(0x13, `REVERSE (SHIFT lever) ${d}`),
			note(0x15, `LOAD (SHIFT) ${d}`),
			note(0x30, `Knob bank SELECT ${d}`),
			note(0x32, `Jog touch, VINYL mode ${d}`),
			note(0x33, `Jog stop, VINYL mode ${d}`),
			note(0x60, `Jog BEND +, CD mode ${d}`),
			note(0x61, `Jog BEND -, CD mode ${d}`),
			note(0x62, `Jog stop, CD mode ${d}`),
			note(0x66, `TRACK browse down ${d}`),
			note(0x67, `TRACK browse up ${d}`),
			note(0x68, `FX SELECT down (SHIFT) ${d}`),
			note(0x69, `FX SELECT up (SHIFT) ${d}`),
			cc(0x0f, `FX KNOB 2 ${d}`),
			cc(0x10, `FX KNOB 3 ${d}`),
			cc(0x11, `FX KNOB 4 ${d}`),
			cc(0x1b, `GAIN (SHIFT) ${d}`),
			cc(0x1c, `HIGH (SHIFT) ${d}`),
			cc(0x1d, `MID (SHIFT) ${d}`),
			cc(0x1e, `LOW (SHIFT) ${d}`),
			cc(0x23, `PITCH coarse (SHIFT) ${d}`),
			cc(0x25, `DECK VOLUME (SHIFT) ${d}`),
			cc(0x29, `Jog SEARCH (SHIFT) ${d}`)
		];
	}

	return [
		note(0x16, `Button bank SELECT ${d}`),
		note(0x17, `LOOP / IN ${d}`),
		note(0x18, `LOOP OUT ${d}`),
		note(0x19, `<< MOVE L. ${d}`),
		note(0x1a, `MOVE L. >> ${d}`),
		note(0x1b, `FX BUT. 1 ${d}`),
		note(0x1c, `FX BUT. 2 ${d}`),
		note(0x1d, `FX BUT. 3 ${d}`),
		note(0x1e, `FX BUT. 4 ${d}`),
		note(0x23, `SHIFT ${d}`),
		note(0x25, `CUE > (SHIFT) ${d}`),
		note(0x27, `SET (SHIFT) ${d}`),
		note(0x29, `LOAD (SHIFT) ${d}`),
		note(0x2a, `JOG MODE lever VINYL ${d}`),
		note(0x2b, `REVERSE (SHIFT lever) ${d}`),
		note(0x31, `Knob bank SELECT ${d}`),
		note(0x36, `Jog touch, VINYL mode ${d}`),
		note(0x37, `Jog stop, VINYL mode ${d}`),
		note(0x63, `Jog BEND +, CD mode ${d}`),
		note(0x64, `Jog BEND -, CD mode ${d}`),
		note(0x65, `Jog stop, CD mode ${d}`),
		note(0x6a, `TRACK browse down ${d}`),
		note(0x6b, `TRACK browse up ${d}`),
		note(0x6c, `FX SELECT down (SHIFT) ${d}`),
		note(0x6d, `FX SELECT up (SHIFT) ${d}`),
		cc(0x13, `FX KNOB 2 ${d}`),
		cc(0x14, `FX KNOB 3 ${d}`),
		cc(0x15, `FX KNOB 4 ${d}`),
		cc(0x1f, `GAIN (SHIFT) ${d}`),
		cc(0x20, `HIGH (SHIFT) ${d}`),
		cc(0x21, `MID (SHIFT) ${d}`),
		cc(0x22, `LOW (SHIFT) ${d}`),
		cc(0x24, `PITCH coarse (SHIFT) ${d}`),
		cc(0x26, `DECK VOLUME (SHIFT) ${d}`),
		cc(0x2d, `Jog SEARCH (SHIFT) ${d}`)
	];
}

const _GLOBAL_HINTS: ControlHint[] = [
	{
		source: { ch: CH, kind: 'cc', id: 0x27 },
		label: 'X-FADER (SHIFT)'
	}
];

// Plain literal matches [MAN] port "Kontrol Dj Midi In: Port 1" (case-insensitive).
// The KDJ500 enumerates over USB only through Konverter's virtual MIDI port.
export const KONTROL_DJ_KDJ500_MAP: DeviceMap = {
	vendor: 'Kontrol Dj',
	nameMatch: 'Kontrol Dj',
	bindings: [..._deckBindings(1), ..._deckBindings(2), ..._GLOBAL_BINDINGS],
	hints: [..._deckHints(1), ..._deckHints(2), ..._GLOBAL_HINTS]
};

export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{ key: 'KONTROL_DJ_KDJ500_MAP', map: KONTROL_DJ_KDJ500_MAP, live: true }
];
