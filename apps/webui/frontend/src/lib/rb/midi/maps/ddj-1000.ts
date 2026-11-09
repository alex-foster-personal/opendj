/**
 * Pioneer DJ DDJ-1000 P0 device map (build unit: DDJ-1000 practice replica, epic #9366).
 *
 * SOURCE OF TRUTH for EVERY Note/CC number in this file: the official
 * Pioneer DJ "DDJ-1000 List of MIDI message" PDF (version 1.00, E1, Dec 2017),
 * https://downloads.support.alphatheta.com/software_info/dj-controllers/DDJ-1000/DDJ-1000_MIDI_Message_List_E1.pdf
 * (not redistributed here; sha256
 * 28d68d514774daa81e603d9a8709105283f38fbc944650671971ecf57cb95d11), cited as
 * [PDF] with the sheet's own Fig. ids (D1..D19 deck, P1..P14 pads, M1..M22 mixer,
 * F1..F11 effect, B1..B3 browser). -L / -R suffixes on the sheet name the left and
 * right physical deck; the wire numbers are the same, only the channel differs.
 *
 * DO NOT source numbers from tools/deck-diagrams/devices/ddj-1000/midi.json: the
 * diagram stub is not the transcription of record.
 *
 * Channel layout ([PDF] p.1 "MIDI channel assignment"):
 *   decks 1-4 (non-pad)          -> channels 1-4
 *   EFFECT                       -> channel 5
 *   BROWSER + global mixer       -> channel 7
 *   performance pads, unshifted  -> channels 8/10/12/14 (deck 1/2/3/4)
 *   performance pads, shifted    -> channels 9/11/13/15
 *
 * SHIFT NOTE: like the FLX10 and FLX4, the DDJ-1000 encodes SHIFT in hardware:
 * a shifted control sends a different data1 (PLAY 11 -> 71, [PDF] D1), and pads
 * move to their odd channel. The SHIFT button itself binds shift_modifier so
 * midiState.shiftHeld stays truthful; shifted rows are their own sources.
 *
 * 14-BIT MIXER CAVEAT: every DDJ-1000 fader and knob is an MSB/LSB CC pair
 * ([PDF] M1-M6, M8, M10, M12-M15, M20, F1, F10). The P0 contract pairs 14-bit
 * CCs for deck_pitch only, so mixer controls bind their MSB CC and the LSB CCs
 * (36/39/43/47/51 on ch 1-4, 63/40/41/44/45/55-58 on ch 7, 34 on ch 5) land in the
 * learn log as unmapped, loud by design.
 *
 * COLOR FX knob ([PDF] F1-1..F1-4, ch 7 CC 23/24/25/26): bound as mixer_channel
 * filter per deck, the same software reading the FLX4 map gives its CFX knob
 * (the hardware applies whichever COLOR FX button is lit; FILTER is the default).
 *
 * OUT OF CONTRACT (hints only, every wire cited): LOOP IN/OUT, MASTER TEMPO,
 * DECK SELECT, KEY RESET, QUANTIZE, SLIP, SLIP REVERSE, SEARCH, MEMORY, pad-mode
 * buttons other than HOT CUE, PAGE buttons, BACK/VIEW, jog touch and +SEARCH/+SHIFT jog rows, COLOR FX
 * select buttons, Beat FX section (BEAT </>, FX SELECT, CH SELECT, LEVEL/DEPTH,
 * FX ON/OFF), BOOTH LEVEL, MIC EQ, SAMPLER CUE/VOL, every +SHIFT twin.
 * Pad modes other than HOT CUE and BEAT LOOP are not bound.
 */

import type {
	ControlHint,
	DeviceMap,
	DeviceMapEntry,
	LedRule,
	MidiBinding
} from '$lib/rb/midi/midi-types';
import type { DeckId } from '$lib/rb/deck-slots';
import {
	pioneerCc,
	pioneerDeckBindings,
	pioneerDeckChannel,
	pioneerNote,
	pioneerPadBindings,
	pioneerPadChannel,
	pioneerPadLedRules,
	pioneerTransportLedRules
} from './pioneer-deck-bindings';

const DECKS: readonly DeckId[] = [1, 2, 3, 4];

/** Beats per BEAT LOOP pad 1..8: our software ladder, as on the FLX10/FLX4. The
 * [PDF] documents only the wire notes (96-103, P1..P8 BEAT LOOP mode PAGE 1). */
export const DDJ1000_BEAT_LOOP_PAD_BEATS: readonly number[] = [0.25, 0.5, 1, 2, 4, 8, 16, 32];
/** [PDF] pad MIDI-OUT: "lit in color specified in color number (1-127). OFF=0x00". */
export const DDJ1000_PAD_ON = 0x7f;
export const DDJ1000_PAD_OFF = 0x00;

/** [PDF] p.1 channel table: EFFECT -> channel 5, BROWSER + global mixer -> channel 7. */
const FX_CH = 5;
const GLOBAL_CH = 7;
/** [PDF] F1-1..F1-4 COLOR FX Parameter MSB CC per channel, all on channel 7. */
const COLOR_FX_CC: Record<DeckId, number> = { 1: 0x17, 2: 0x18, 3: 0x19, 4: 0x1a };

// -------------------------------------------------------------- per-deck rows

function _deckBindings(deck: DeckId): MidiBinding[] {
	const ch = pioneerDeckChannel(deck);
	const jog = (cc: number): MidiBinding => ({
		source: pioneerCc(ch, cc),
		action: { type: 'deck_jog', deck },
		relative: true,
		relativeCentered: true
	});
	return [
		// [PDF] D1 PLAY/PAUSE note 11, D2 CUE note 12, D19 SHIFT note 63, D4 Tempo CC 0 MSB /
		// CC 32 LSB, D12 4 BEAT LOOP/EXIT note 20 and its +SHIFT twin note 80 (0x50).
		...pioneerDeckBindings(deck, 0x50),
		// [PDF] D3 JOG (Platter): CC 34 vinyl on, CC 35 vinyl off, wheel side CC 33.
		jog(0x22),
		jog(0x23),
		jog(0x21),
		// [PDF] D7 BEAT SYNC note 88.
		{ source: pioneerNote(ch, 0x58), action: { type: 'deck_sync_toggle', deck } },
		// [PDF] D8 KEY SYNC note 101.
		{ source: pioneerNote(ch, 0x65), action: { type: 'deck_key_sync_toggle', deck } },
		// [PDF] P9 HOT CUE pad-mode button note 27 (0x1B); PAD FX 1, BEAT JUMP, SAMPLER and the
		// PAGE buttons stay hints (no matching pad mode in the glue yet).
		{
			source: pioneerNote(ch, 0x1b),
			action: { type: 'controller_pad_mode', deck, mode: 'hot_cue' }
		}
	];
}

function _mixerBindings(deck: DeckId): MidiBinding[] {
	const ch = pioneerDeckChannel(deck);
	return [
		// [PDF] M3 GAIN (TRIM) CC 4 MSB.
		{ source: pioneerCc(ch, 0x04), action: { type: 'mixer_channel', deck, target: 'trim' } },
		// [PDF] M4 EQ HI CC 7, M5 EQ MID CC 11, M6 EQ LOW CC 15 (MSB).
		{
			source: pioneerCc(ch, 0x07),
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'high' }
		},
		{
			source: pioneerCc(ch, 0x0b),
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'mid' }
		},
		{
			source: pioneerCc(ch, 0x0f),
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'low' }
		},
		// [PDF] M2 CH FADER CC 19 MSB, min at bottom.
		{ source: pioneerCc(ch, 0x13), action: { type: 'mixer_channel', deck, target: 'fader' } },
		// [PDF] F1 COLOR FX Parameter, channel 7 CC 23-26 (see header).
		{
			source: pioneerCc(GLOBAL_CH, COLOR_FX_CC[deck]),
			action: { type: 'mixer_channel', deck, target: 'filter' }
		},
		// [PDF] M7 CUE (Headphone) note 84.
		{ source: pioneerNote(ch, 0x54), action: { type: 'channel_cue', deck } }
	];
}

function _padBindings(deck: DeckId): MidiBinding[] {
	// [PDF] P1..P8 HOT CUE mode PAGE 1 notes 0-7, BEAT LOOP mode PAGE 1 notes 96-103.
	return pioneerPadBindings(deck, DDJ1000_BEAT_LOOP_PAD_BEATS);
}

// -------------------------------------------------- browser + global mixer

function _browserAndGlobalBindings(): MidiBinding[] {
	const ch = GLOBAL_CH;
	return [
		// [PDF] B1 Rotary Selector rotate CC 64, +SHIFT CC 100, relative ticks.
		{ source: pioneerCc(ch, 0x40), action: { type: 'browse_encoder' }, relative: true },
		{ source: pioneerCc(ch, 0x64), action: { type: 'browse_encoder' }, relative: true },
		// [PDF] B1 press: deck 1 note 70, deck 2 note 71, deck 3 note 72, deck 4 note 73.
		{ source: pioneerNote(ch, 0x46), action: { type: 'browse_load', deck: 1 } },
		{ source: pioneerNote(ch, 0x47), action: { type: 'browse_load', deck: 2 } },
		{ source: pioneerNote(ch, 0x48), action: { type: 'browse_load', deck: 3 } },
		{ source: pioneerNote(ch, 0x49), action: { type: 'browse_load', deck: 4 } },
		// [PDF] M1 CROSSFADER CC 31 MSB, min left; M8 MASTER LEVEL CC 8 MSB.
		{ source: pioneerCc(ch, 0x1f), action: { type: 'mixer_global', target: 'crossfader' } },
		{ source: pioneerCc(ch, 0x08), action: { type: 'mixer_global', target: 'master' } },
		// [PDF] M12 HEADPHONES MIXING CC 12 MSB, M13 HEADPHONES LEVEL CC 13 MSB.
		{ source: pioneerCc(ch, 0x0c), action: { type: 'headphone_mix' } },
		{ source: pioneerCc(ch, 0x0d), action: { type: 'headphone_level' } },
		// [PDF] M9 MASTER CUE note 99.
		{ source: pioneerNote(ch, 0x63), action: { type: 'master_cue', mode: 'latch' } }
	];
}

// ----------------------------------------------------------------- hints

/** Documented-but-unbound deck rows ([PDF] D1-D19 and the +SHIFT column). */
function _deckHints(deck: DeckId): ControlHint[] {
	const ch = pioneerDeckChannel(deck);
	const d = `deck ${deck}`;
	// [PDF] D6 DECK SELECT: the press sends note 114 on the channel of the deck being
	// switched TO (1/3 on the left deck, 2/4 on the right); +SHIFT is ch 7 note 124/125.
	const selectCh = deck <= 2 ? deck + 2 : deck - 2;
	return [
		{ source: pioneerNote(ch, 0x47), label: `PLAY/PAUSE + SHIFT (${d})` },
		{ source: pioneerNote(ch, 0x48), label: `CUE + SHIFT (${d})` },
		{ source: pioneerNote(ch, 0x36), label: `JOG touch (${d})` },
		{ source: pioneerNote(ch, 0x67), label: `JOG touch + SHIFT (${d})` },
		{ source: pioneerCc(ch, 0x29), label: `JOG platter + SEARCH (${d})` },
		{ source: pioneerCc(ch, 0x1f), label: `JOG platter + SHIFT (${d})` },
		{ source: pioneerCc(ch, 0x26), label: `JOG wheel side + SHIFT (${d})` },
		{ source: pioneerCc(ch, 0x05), label: `Tempo + SHIFT (${d})` },
		{ source: pioneerNote(ch, 0x1a), label: `MASTER TEMPO (${d})` },
		{ source: pioneerNote(ch, 0x60), label: `MASTER TEMPO + SHIFT (${d})` },
		{ source: pioneerNote(selectCh, 0x72), label: `DECK SELECT (${d})` },
		{ source: pioneerNote(ch, 0x5c), label: `BEAT SYNC + SHIFT (${d})` },
		{ source: pioneerNote(ch, 0x1c), label: `KEY SYNC + SHIFT (${d})` },
		{ source: pioneerNote(ch, 0x64), label: `KEY RESET (${d})` },
		{ source: pioneerNote(ch, 0x1f), label: `KEY RESET + SHIFT (${d})` },
		{ source: pioneerNote(ch, 0x10), label: `LOOP IN (${d})` },
		{ source: pioneerNote(ch, 0x4c), label: `LOOP IN + SHIFT (${d})` },
		{ source: pioneerNote(ch, 0x11), label: `LOOP OUT (${d})` },
		{ source: pioneerNote(ch, 0x4d), label: `LOOP OUT + SHIFT (${d})` },
		{ source: pioneerNote(ch, 0x35), label: `QUANTIZE (${d})` },
		{ source: pioneerNote(ch, 0x39), label: `QUANTIZE + SHIFT (${d})` },
		{ source: pioneerNote(ch, 0x40), label: `SLIP (${d})` },
		{ source: pioneerNote(ch, 0x17), label: `SLIP + SHIFT (${d})` },
		{ source: pioneerNote(ch, 0x15), label: `SLIP REVERSE (${d})` },
		{ source: pioneerNote(ch, 0x38), label: `SLIP REVERSE + SHIFT (${d})` },
		{ source: pioneerNote(ch, 0x5e), label: `SEARCH back (${d})` },
		{ source: pioneerNote(ch, 0x70), label: `SEARCH back long press (${d})` },
		{ source: pioneerNote(ch, 0x51), label: `SEARCH back + SHIFT (${d})` },
		{ source: pioneerNote(ch, 0x5f), label: `SEARCH forward (${d})` },
		{ source: pioneerNote(ch, 0x71), label: `SEARCH forward long press (${d})` },
		{ source: pioneerNote(ch, 0x53), label: `SEARCH forward + SHIFT (${d})` },
		{ source: pioneerNote(ch, 0x3d), label: `MEMORY (${d})` },
		{ source: pioneerNote(ch, 0x3e), label: `MEMORY + SHIFT (${d})` },
		// [PDF] M7 CUE (Headphone) + SHIFT note 104; M2 fader start notes 102/82.
		{ source: pioneerNote(ch, 0x68), label: `CH CUE + SHIFT (${d})` },
		{ source: pioneerNote(ch, 0x66), label: `CH FADER start PLAY (${d})` },
		{ source: pioneerNote(ch, 0x52), label: `CH FADER start CUE (${d})` }
	];
}

/** [PDF] P9-P12 pad mode buttons, P13/P14 PAGE buttons (HOT CUE mode rows), ch 1-4. */
function _padModeHints(deck: DeckId): ControlHint[] {
	const ch = pioneerDeckChannel(deck);
	const d = `deck ${deck}`;
	return [
		{ source: pioneerNote(ch, 0x69), label: `HOT CUE MODE + SHIFT (${d})` },
		{ source: pioneerNote(ch, 0x1e), label: `PAD FX 1 MODE (${d})` },
		{ source: pioneerNote(ch, 0x6b), label: `PAD FX 1 MODE + SHIFT (${d})` },
		{ source: pioneerNote(ch, 0x20), label: `BEAT JUMP MODE (${d})` },
		{ source: pioneerNote(ch, 0x6d), label: `BEAT JUMP MODE + SHIFT (${d})` },
		{ source: pioneerNote(ch, 0x22), label: `SAMPLER MODE (${d})` },
		{ source: pioneerNote(ch, 0x6f), label: `SAMPLER MODE + SHIFT (${d})` },
		{ source: pioneerNote(ch, 0x24), label: `PAGE back, HOT CUE mode (${d})` },
		{ source: pioneerNote(ch, 0x01), label: `PAGE back + SHIFT, HOT CUE mode (${d})` },
		{ source: pioneerNote(ch, 0x2c), label: `PAGE forward, HOT CUE mode (${d})` },
		{ source: pioneerNote(ch, 0x09), label: `PAGE forward + SHIFT, HOT CUE mode (${d})` }
	];
}

/** [PDF] P1-P8 + SHIFT: the same note on the deck's odd pad channel (HOT CUE mode PAGE 1). */
function _shiftedPadHints(deck: DeckId): ControlHint[] {
	const ch = pioneerPadChannel(deck) + 1;
	return Array.from({ length: 8 }, (_, pad) => ({
		source: pioneerNote(ch, pad),
		label: `PAD ${pad + 1} + SHIFT (deck ${deck})`
	}));
}

function _globalHints(): ControlHint[] {
	const ch = GLOBAL_CH;
	const fx = FX_CH;
	const fxSelect: [number, string][] = [
		[0x20, 'LOW CUT ECHO'],
		[0x21, 'ECHO'],
		[0x22, 'MULTI TAP DELAY'],
		[0x23, 'SPIRAL'],
		[0x24, 'REVERB'],
		[0x25, 'TRANS'],
		[0x26, 'ENIGMA JET'],
		[0x27, 'FLANGER'],
		[0x28, 'PHASER'],
		[0x29, 'PITCH'],
		[0x2a, 'SLIP ROLL'],
		[0x2b, 'ROLL'],
		[0x2c, 'MOBIUS SAW'],
		[0x2d, 'MOBIUS TRIANGLE']
	];
	const chSelect: [number, string][] = [
		[0x10, 'CH1'],
		[0x11, 'CH2'],
		[0x12, 'CH3'],
		[0x13, 'CH4'],
		[0x14, 'MASTER'],
		[0x15, 'MIC'],
		[0x16, 'SAMPLER']
	];
	return [
		// [PDF] B1 press + SHIFT per deck; B2 BACK; B3 VIEW.
		{ source: pioneerNote(ch, 0x5d), label: 'LOAD deck 1 + SHIFT' },
		{ source: pioneerNote(ch, 0x6d), label: 'LOAD deck 2 + SHIFT' },
		{ source: pioneerNote(ch, 0x5e), label: 'LOAD deck 3 + SHIFT' },
		{ source: pioneerNote(ch, 0x6f), label: 'LOAD deck 4 + SHIFT' },
		{ source: pioneerNote(ch, 0x65), label: 'BACK' },
		{ source: pioneerNote(ch, 0x66), label: 'BACK + SHIFT' },
		{ source: pioneerNote(ch, 0x7a), label: 'VIEW' },
		{ source: pioneerNote(ch, 0x67), label: 'VIEW long press' },
		{ source: pioneerNote(ch, 0x68), label: 'VIEW + SHIFT' },
		// [PDF] D6 DECK SELECT + SHIFT: left deck note 124, right deck note 125.
		{ source: pioneerNote(ch, 0x7c), label: 'DECK SELECT 1/3 + SHIFT' },
		{ source: pioneerNote(ch, 0x7d), label: 'DECK SELECT 2/4 + SHIFT' },
		// [PDF] M9 MASTER CUE + SHIFT; M10 BOOTH LEVEL; M14/M15 MIC EQ; M19/M20 SAMPLER.
		{ source: pioneerNote(ch, 0x62), label: 'MASTER CUE + SHIFT' },
		{ source: pioneerCc(ch, 0x09), label: 'BOOTH LEVEL' },
		{ source: pioneerCc(ch, 0x07), label: 'MIC EQ HI' },
		{ source: pioneerCc(ch, 0x0f), label: 'MIC EQ LOW' },
		{ source: pioneerNote(ch, 0x69), label: 'SAMPLER CUE' },
		{ source: pioneerNote(ch, 0x6e), label: 'SAMPLER CUE + SHIFT' },
		{ source: pioneerCc(ch, 0x03), label: 'SAMPLER VOL' },
		// [PDF] F2-F5 COLOR FX select buttons, ch 7 notes 0-3, +SHIFT 8-11.
		{ source: pioneerNote(ch, 0x00), label: 'COLOR FX DUB ECHO' },
		{ source: pioneerNote(ch, 0x08), label: 'COLOR FX DUB ECHO + SHIFT' },
		{ source: pioneerNote(ch, 0x01), label: 'COLOR FX PITCH' },
		{ source: pioneerNote(ch, 0x09), label: 'COLOR FX PITCH + SHIFT' },
		{ source: pioneerNote(ch, 0x02), label: 'COLOR FX NOISE' },
		{ source: pioneerNote(ch, 0x0a), label: 'COLOR FX NOISE + SHIFT' },
		{ source: pioneerNote(ch, 0x03), label: 'COLOR FX FILTER' },
		{ source: pioneerNote(ch, 0x0b), label: 'COLOR FX FILTER + SHIFT' },
		// [PDF] F6/F7 BEAT < >, F8 FX SELECT, F9 CH SELECT, F10 LEVEL/DEPTH, F11 FX ON/OFF: ch 5.
		{ source: pioneerNote(fx, 0x4a), label: 'BEAT FX beat back' },
		{ source: pioneerNote(fx, 0x66), label: 'BEAT FX beat back + SHIFT' },
		{ source: pioneerNote(fx, 0x4b), label: 'BEAT FX beat forward' },
		{ source: pioneerNote(fx, 0x6b), label: 'BEAT FX beat forward + SHIFT' },
		...fxSelect.map(([id, name]): ControlHint => ({
			source: pioneerNote(fx, id),
			label: `FX SELECT ${name}`
		})),
		...chSelect.map(([id, name]): ControlHint => ({
			source: pioneerNote(fx, id),
			label: `FX CH SELECT ${name}`
		})),
		{ source: pioneerCc(fx, 0x02), label: 'FX LEVEL/DEPTH' },
		{ source: pioneerNote(fx, 0x47), label: 'FX ON/OFF' },
		{ source: pioneerNote(fx, 0x43), label: 'FX ON/OFF + SHIFT' }
	];
}

// -------------------------------------------------------------- led rules

function _ledRules(deck: DeckId): LedRule[] {
	const ch = pioneerDeckChannel(deck);
	// [PDF] MIDI-OUT columns: D1 PLAY note 11, D2 CUE note 12, D12 4 BEAT LOOP/EXIT note 20,
	// D7 BEAT SYNC note 88, M7 CUE (Headphone) note 84, P9 HOT CUE mode note 27, P1-P8 pad
	// notes with colour velocity. The sheet also prints MIDI-OUT for MASTER TEMPO, KEY SYNC,
	// LOOP IN/OUT, QUANTIZE, SLIP, SLIP REVERSE, MEMORY, MASTER CUE, SAMPLER CUE, BEAT < >,
	// FX ON/OFF and the other pad-mode buttons; LedTrigger has no selector for those states.
	return [
		...pioneerTransportLedRules(deck, 0x14),
		{
			trigger: { kind: 'beat_sync_enabled', deck },
			out: { ch, note: 0x58, velocityOn: 0x7f, velocityOff: 0x00 }
		},
		{
			trigger: { kind: 'channel_cue_enabled', deck },
			out: { ch, note: 0x54, velocityOn: 0x7f, velocityOff: 0x00 }
		},
		{
			trigger: { kind: 'pad_mode_selected', deck, mode: 'hot_cue' },
			out: { ch, note: 0x1b, velocityOn: 0x7f, velocityOff: 0x00 }
		},
		...pioneerPadLedRules(deck, DDJ1000_PAD_ON, DDJ1000_PAD_OFF)
	];
}

// ------------------------------------------------------------- device map

export const DDJ1000_MAP: DeviceMap = {
	vendor: 'Pioneer DJ',
	// WebMIDI port names carry the product name, e.g. "DDJ-1000". The SRT variant is
	// a different unit with its own sheet, so the match stays exact.
	nameMatch: 'DDJ-1000(?:$|[^S])',
	bindings: [
		...DECKS.flatMap((deck) => _deckBindings(deck)),
		...DECKS.flatMap((deck) => _mixerBindings(deck)),
		...DECKS.flatMap((deck) => _padBindings(deck)),
		..._browserAndGlobalBindings()
	],
	leds: DECKS.flatMap((deck) => _ledRules(deck)),
	hints: [
		...DECKS.flatMap((deck) => _deckHints(deck)),
		...DECKS.flatMap((deck) => _padModeHints(deck)),
		...DECKS.flatMap((deck) => _shiftedPadHints(deck)),
		..._globalHints()
	]
};

/** Self-registration: maps/entries.ts discovers this export, so no shared list names this map. */
export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{ key: 'DDJ1000_MAP', map: DDJ1000_MAP, live: true }
];
