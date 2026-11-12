/**
 * Hercules DJControl Inpulse 200 device map (issue #10846, SPRINT400-A20).
 *
 * SOURCES (clean room: wires transcribed from the manufacturer chart only;
 * the Mixxx GPL map was not used as a source):
 *   [CHART] "MIDI commands of Hercules DJControl Inpulse 200", V1.0,
 *        Guillemot Corp, sections "1) MIDI INPUT" (a: Note On, b: Control
 *        Change) and "2) MIDI OUTPUT".
 *        https://ts.hercules.com/download/sound/manuals/DJC_Inpulse200/DJControlInpulse200_MIDI_Commands_V_1_0.pdf
 *        sha256 7be2dd081f87210ec86a80e632748f527b81fe681770a8ccfe020b2c4e6470fb
 *   [MANUAL] DJControl Inpulse 200 User Manual (EN), sections MODE, LOOP,
 *        MASTER and item 9 (dual filters).
 *        https://ts.hercules.com/download/sound/manuals/DJC_Inpulse200/DJControl_Inpulse_200_User_Manual_EN.pdf
 *        sha256 ce0e772fccdb14bb1daadc6a5e39345a93a61260dbf7395a0f5b7e306907eb40
 *
 * hardware_confirmed: false (no unit on hand; wires from [CHART] only).
 *
 * Wire convention: [CHART] prints status 9n/Bn with a 0-based channel nibble;
 * `ch` here is 1-based, so ch = n + 1. ch 1 global, ch 2 deck A, ch 3 deck B,
 * ch 4 global+SHIFT, ch 5 deck A+SHIFT, ch 6 deck B+SHIFT CCs, ch 7/8 pads.
 * SHIFT is a hardware layer (the unit moves shifted controls to other
 * channels), so no binding takes `shift: true`. Encoders and jogs send
 * 01..3F CW / 7F..40 CCW (two's complement): relative, not relativeCentered.
 *
 * Requirements (mini-PRD):
 *   ✔︎ Transport, sync, PFL, load, 4 hot cues, manual and 4-beat loop per deck.
 *     [if] PLAY deck A is not note ch2 0x07 [then ⛔️] wire drift from [CHART]
 *     [if] a binding (ch, kind, id) is absent from [CHART] [then ⛔️] fabricated
 *   ✔︎ Mixer: fader, 2-band EQ (LOW CC 02, HIGH CC 04, no mid), GAIN trim,
 *     FILTER (the colour FX) per deck, crossfader, browse encoder.
 *     [if] FILTER CC 01 is not mixer_channel filter [then ⛔️] colour FX unbound
 *   ✔︎ Pitch is a 14-bit pair (MSB 08, LSB 28): deck_pitch lsbOffset 0x20.
 *     Orientation unverified until hardware, so no `invert`.
 *   ✔︎ Mixer and EQ LSBs (Bn 20/21/22/24/25, B0 20) stay unbound, as ddj-400.ts.
 *   → MODE3 FX pads: DJUCED transition macros; MidiAction has no beat-FX
 *     action (contract change outside #10846), so hinted only.
 *   → ROLL pads: a roll is momentary and deck_beat_loop latches, so hinted only.
 *   → VOL_MASTER / VOL_HDP (B0 03/04): [MANUAL] calls them hardware settings;
 *     a software bind would attenuate twice, so hinted only.
 *   → Chart typo: Deck B + Shift notes print status 0x94, the same as Deck A +
 *     Shift. Nothing binds on ch 5 notes; each is hinted once naming both decks.
 */

import type {
	ControlHint,
	DeviceMap,
	DeviceMapEntry,
	LedRule,
	MidiBinding
} from '$lib/rb/midi/midi-types';

export const HERCULES_INPULSE_200_HARDWARE_CONFIRMED = false as const;

// --------------------------------------------------------------- bindings

/** Per-deck rows: deck A on ch 2 (pads ch 7), deck B on ch 3 (pads ch 8). */
function _deckBindings(ch: number, padCh: number, deck: 1 | 2): MidiBinding[] {
	const slots = ['A', 'B', 'C', 'D'] as const;
	return [
		// [CHART] 1a PLAY 9n 07
		{ source: { ch, kind: 'note', id: 0x07 }, action: { type: 'deck_play_toggle', deck } },
		// [CHART] 1a CUE 9n 06
		{ source: { ch, kind: 'note', id: 0x06 }, action: { type: 'deck_cue', deck } },
		// [CHART] 1a SYNC 9n 05
		{ source: { ch, kind: 'note', id: 0x05 }, action: { type: 'deck_sync_toggle', deck } },
		// [CHART] 1a SHIFT 9n 04 (keeps midiState.shiftHeld truthful)
		{ source: { ch, kind: 'note', id: 0x04 }, action: { type: 'shift_modifier' } },
		// [CHART] 1a LOOP_IN 9n 09 / LOOP_OUT 9n 0A -> one-button loop cycle
		{ source: { ch, kind: 'note', id: 0x09 }, action: { type: 'deck_manual_loop_cycle', deck } },
		{ source: { ch, kind: 'note', id: 0x0a }, action: { type: 'deck_manual_loop_cycle', deck } },
		// [CHART] 1a LOOP_IN long press 9n 0B; [MANUAL] LOOP: 4-beat auto loop
		{
			source: { ch, kind: 'note', id: 0x0b },
			action: { type: 'deck_beat_loop', deck, beats: 4 }
		},
		// [CHART] 1a PFL 9n 0C
		{ source: { ch, kind: 'note', id: 0x0c }, action: { type: 'channel_cue', deck } },
		// [CHART] 1a LOAD 9n 0D
		{ source: { ch, kind: 'note', id: 0x0d }, action: { type: 'browse_load', deck } },
		// [CHART] 1a pads MODE1 (HOTCUE) 96/97 00..03
		...slots.map((slot, i): MidiBinding => ({
			source: { ch: padCh, kind: 'note', id: 0x00 + i },
			action: { type: 'deck_hot_cue', deck, slot }
		})),
		// [CHART] 1b VOL Bn 00
		{
			source: { ch, kind: 'cc', id: 0x00 },
			action: { type: 'mixer_channel', deck, target: 'fader' }
		},
		// [CHART] 1b FILTER Bn 01 ([MANUAL] item 9: dual filters, the colour FX)
		{
			source: { ch, kind: 'cc', id: 0x01 },
			action: { type: 'mixer_channel', deck, target: 'filter' }
		},
		// [CHART] 1b LOW Bn 02
		{
			source: { ch, kind: 'cc', id: 0x02 },
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'low' }
		},
		// [CHART] 1b HIGH Bn 04 (no CC 03: 2-band EQ)
		{
			source: { ch, kind: 'cc', id: 0x04 },
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'high' }
		},
		// [CHART] 1b GAIN Bn 05
		{
			source: { ch, kind: 'cc', id: 0x05 },
			action: { type: 'mixer_channel', deck, target: 'trim' }
		},
		// [CHART] 1b PITCH Bn 08 MSB / Bn 28 LSB
		{ source: { ch, kind: 'cc', id: 0x08 }, action: { type: 'deck_pitch', deck, lsbOffset: 0x20 } },
		// [CHART] 1b JOG Bn 09 and JOG_SCRATCH Bn 0A (two's complement)
		{ source: { ch, kind: 'cc', id: 0x09 }, action: { type: 'deck_jog', deck }, relative: true },
		{ source: { ch, kind: 'cc', id: 0x0a }, action: { type: 'deck_jog', deck }, relative: true }
	];
}

/** Global rows on ch 1. */
const _GLOBAL_BINDINGS: MidiBinding[] = [
	// [CHART] 1b XFADER B0 00
	{
		source: { ch: 1, kind: 'cc', id: 0x00 },
		action: { type: 'mixer_global', target: 'crossfader' }
	},
	// [CHART] 1b BROWSE_ENC B0 01
	{ source: { ch: 1, kind: 'cc', id: 0x01 }, action: { type: 'browse_encoder' }, relative: true },
	// [CHART] 1a PFL_MASTER 90 02 ([MANUAL] MASTER: toggles mix in headphones)
	{ source: { ch: 1, kind: 'note', id: 0x02 }, action: { type: 'master_cue', mode: 'latch' } }
];

// -------------------------------------------------------- documented hints

const _PAD_HINTS: ReadonlyArray<{ id: number; label: string }> = [
	...['1/16', '1/8', '1/4', '1/2'].map((len, i) => ({ id: 0x10 + i, label: `ROLL ${len}` })),
	...[1, 2, 3, 4].map((n, i) => ({ id: 0x20 + i, label: `FX pad ${n} (DJUCED transition FX)` })),
	...[1, 2, 3, 4].map((n, i) => ({ id: 0x30 + i, label: `SAMPLER pad ${n}` })),
	...[0x08, 0x18, 0x28, 0x38].flatMap((base) =>
		[0, 1, 2, 3].map((i) => ({
			id: base + i,
			label: `SHIFT + pad ${i + 1} (mode bank ${base >> 4})`
		}))
	)
];

/** [CHART] rows this map does not bind, named so learn-log traffic reads well. */
function _deckHints(ch: number, padCh: number, shiftCcCh: number, deck: 1 | 2): ControlHint[] {
	const d = `deck ${deck}`;
	return [
		// [CHART] 1a VINYL 9n 03, JOG_TOUCH 9n 08, LOAD long 9n 0E, MODE1..4 9n 0F..12
		{ source: { ch, kind: 'note', id: 0x03 }, label: `VINYL (${d})` },
		{ source: { ch, kind: 'note', id: 0x08 }, label: `JOG TOUCH (${d})` },
		{ source: { ch, kind: 'note', id: 0x0e }, label: `LOAD long press (${d})` },
		...['HOTCUE', 'ROLL', 'FX', 'SAMPLER'].map((mode, i): ControlHint => ({
			source: { ch, kind: 'note', id: 0x0f + i },
			label: `MODE ${mode} (${d})`
		})),
		// [CHART] 1a pads 96/97 MODE2..4 and SHIFT banks
		..._PAD_HINTS.map((p): ControlHint => ({
			source: { ch: padCh, kind: 'note', id: p.id },
			label: `${p.label} (${d})`
		})),
		// [CHART] 1b SHIFT + JOG B4/B5 09/0A
		{ source: { ch: shiftCcCh, kind: 'cc', id: 0x09 }, label: `SHIFT + JOG search (${d})` },
		{ source: { ch: shiftCcCh, kind: 'cc', id: 0x0a }, label: `SHIFT + JOG scratch search (${d})` }
	];
}

const _SHIFT_DECK_NOTES: ReadonlyArray<{ id: number; name: string }> = [
	{ id: 0x03, name: 'VINYL' },
	{ id: 0x05, name: 'SYNC' },
	{ id: 0x06, name: 'CUE' },
	{ id: 0x07, name: 'PLAY' },
	{ id: 0x08, name: 'JOG TOUCH' },
	{ id: 0x09, name: 'LOOP IN' },
	{ id: 0x0a, name: 'LOOP OUT' },
	{ id: 0x0c, name: 'PFL (deck B) or LOOP IN long (deck A)' },
	{ id: 0x0d, name: 'LOAD (deck B) or PFL (deck A)' }
];

const _GLOBAL_HINTS: ControlHint[] = [
	// [CHART] 1a global 90 00/01/03
	{ source: { ch: 1, kind: 'note', id: 0x00 }, label: 'BROWSE push' },
	{ source: { ch: 1, kind: 'note', id: 0x01 }, label: 'BEATMATCH GUIDE' },
	{ source: { ch: 1, kind: 'note', id: 0x03 }, label: 'ASSISTANT' },
	// [CHART] 1b global B0 02/03/04
	{ source: { ch: 1, kind: 'cc', id: 0x02 }, label: 'BROWSE encoder (assistant)' },
	{ source: { ch: 1, kind: 'cc', id: 0x03 }, label: 'MASTER volume (hardware level)' },
	{ source: { ch: 1, kind: 'cc', id: 0x04 }, label: 'HEADPHONE volume (hardware level)' },
	// [CHART] 1a/1b global + SHIFT 93 00/02, B3 01
	{ source: { ch: 4, kind: 'note', id: 0x00 }, label: 'SHIFT + BROWSE push' },
	{ source: { ch: 4, kind: 'note', id: 0x02 }, label: 'SHIFT + MASTER' },
	{ source: { ch: 4, kind: 'cc', id: 0x01 }, label: 'SHIFT + BROWSE encoder' },
	// [CHART] 1a deck + SHIFT 94 xx (printed for both decks, a chart typo)
	..._SHIFT_DECK_NOTES.map((n): ControlHint => ({
		source: { ch: 5, kind: 'note', id: n.id },
		label: `SHIFT + ${n.name} (deck A or B: [CHART] prints 0x94 for both)`
	}))
];

// ------------------------------------------------------------ led feedback

/** [CHART] 2 MIDI OUTPUT: Note On to the input note, 7F on / 00 off. */
function _deckLeds(ch: number, padCh: number, deck: 1 | 2): LedRule[] {
	const led = (c: number, note: number) => ({ ch: c, note, velocityOn: 0x7f, velocityOff: 0x00 });
	const slots = ['A', 'B', 'C', 'D'] as const;
	return [
		{ trigger: { kind: 'deck_playing', deck }, out: led(ch, 0x07) },
		{ trigger: { kind: 'beat_sync_enabled', deck }, out: led(ch, 0x05) },
		{ trigger: { kind: 'channel_cue_enabled', deck }, out: led(ch, 0x0c) },
		{ trigger: { kind: 'loop_engaged', deck }, out: led(ch, 0x09) },
		{ trigger: { kind: 'loop_engaged', deck }, out: led(ch, 0x0a) },
		...slots.map((slot, i): LedRule => ({
			trigger: { kind: 'hot_cue_present', deck, slot },
			out: led(padCh, i)
		}))
	];
}

// -------------------------------------------------------------- device map

/** Matches `Inpulse 200` unless a whole word starting `MK` follows it, and
 * never `Inpulse 2000` (a word char after 200 fails every branch). Written
 * WITHOUT lookaround: the Rust engine compiles nameMatch with regex_lite.
 * Under the `i` flag `[^M]`/`[^K]` also exclude lower-case m and k. */
const _NAME_MATCH = '\\bInpulse 200(?:$|[^\\w\\s]|\\s+(?:$|[^\\sM]|M(?:$|[^K])))';

export const HERCULES_INPULSE_200_MAP: DeviceMap = {
	vendor: 'Hercules',
	nameMatch: _NAME_MATCH,
	bindings: [..._deckBindings(2, 7, 1), ..._deckBindings(3, 8, 2), ..._GLOBAL_BINDINGS],
	leds: [..._deckLeds(2, 7, 1), ..._deckLeds(3, 8, 2)],
	hints: [..._deckHints(2, 7, 5, 1), ..._deckHints(3, 8, 6, 2), ..._GLOBAL_HINTS]
};

/** Self-registration: maps/entries.ts discovers this export. */
export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{ key: 'HERCULES_INPULSE_200_MAP', map: HERCULES_INPULSE_200_MAP, live: true }
];
