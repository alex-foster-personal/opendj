/**
 * DJ-Tech MIX-101 live device map (catalog id dj-tech-mix-101).
 *
 * SOURCE OF TRUTH for EVERY Note/CC number in this file: DJ-Tech MIX-101 User
 * Manual, MIDI implementation tables on printed pages 28-30 (PDF pages 29-31).
 * ManualsLib: https://www.manualslib.com/manual/1110404/Dj-Tech-Mix-101.html
 * Manualzz doc 57473261: https://manualzz.com/doc/57473261/
 * Chart notation: `9x` = Note On/Off ON 7F / OFF 00; `Bx` = Control Change;
 * encoders send CW 0x41 / CCW 0x3F centred on 0x40.
 *
 * hardware_confirmed: false. No hardware on this issue; polarity is left
 * uninverted until a capture confirms it.
 *
 * Channel: every control binds MIDI channel 1. The chart's MIDI OUT selector
 * (CC 0x0E, values 00-07) retargets the whole device to channels 1-8; that
 * selector is a hint only.
 *
 * DO NOT source numbers from tools/deck-diagrams/devices/dj-tech-mix-101/midi.json
 * or from Mixxx DJ-Tech Mix-101.midi.xml: GPL Mixxx dumps, this repo is Apache 2.0.
 * Mixxx may be read only to cross-check a wire already taken from the chart.
 * Mixxx puts play on notes 7/9, cue on 3/4 and head volume on cc 0x10; the chart
 * puts PLAY on 0x07, CUE on 0x08 and A/B on 0x09. The chart wins.
 *
 * A/B (note 0x09) binds shift_modifier. Holding A/B makes the shared transport
 * section drive deck 2 via shift: true twins on the same chart rows. A/B is a
 * held layer, not the latching focus the hardware LEDs suggest. A latching
 * deck-focus action needs a contract change and is out of scope. LEDs 0x09/0x0A
 * (A/B) have no LedTrigger and stay undriven. K1/K2 LEDs (0x03/0x04) likewise.
 *
 * FX: MidiAction has no beat/colour FX member (#9788). MODE notes bind
 * controller_pad_mode; keys "1"/"2" bind controller_pad pads 1/2. In FX mode
 * the keys reach the pad runtime instant_fx path; the P0 gap noted in the RMX2
 * header applies.
 *
 * Chart defects: the ADC6 (CHA HI) row is printed twice; CHA HI binds once at
 * CC 0x26. CF CURVE is printed on CC 0x23 with the crossfader; 0x23 binds
 * crossfader only. CF CURVE is documented here, not bound or hinted (hints must
 * not collide with bindings).
 *
 * No pitch fader and no SysEx dump on the chart; no deck_pitch or init.
 */

import type {
	ControlHint,
	DeviceMap,
	DeviceMapEntry,
	LedRule,
	MidiBinding,
	MidiSource
} from '$lib/rb/midi/midi-types';
import type { ControllerPadMode } from '$lib/rb/midi/midi-types';

const CH = 1;
const DECKS = [1, 2] as const;
type Deck = (typeof DECKS)[number];

const note = (id: number): MidiSource => ({ ch: CH, kind: 'note', id });
const cc = (id: number): MidiSource => ({ ch: CH, kind: 'cc', id });

/** Shared transport / pad section wire ids from the manual MIDI tables. */
const WIRE = {
	play: 0x07,
	cue: 0x08,
	sync: 0x06,
	k1: 0x03,
	k2: 0x04,
	load: 0x0c,
	jog: 0x13,
	scratch: 0x15,
	abKey: 0x09,
	modeSampler: 0x11,
	modeLoop: 0x12,
	modeFx: 0x13,
	modeCueScratch: 0x14
} as const;

/** Per-channel mixer CC ids (one CC per strip, not shift-layered). */
const MIXER: Record<
	Deck,
	{ fader: number; trim: number; eqHigh: number; eqMid: number; eqLow: number }
> = {
	1: { fader: 0x21, trim: 0x24, eqHigh: 0x26, eqMid: 0x28, eqLow: 0x2a },
	2: { fader: 0x22, trim: 0x25, eqHigh: 0x27, eqMid: 0x29, eqLow: 0x2b }
};

/** [Manual] LED out: ON 7F / OFF 00. */
export const MIX101_LED_ON = 0x7f;
/** [Manual] LED out: ON 7F / OFF 00. */
export const MIX101_LED_OFF = 0x00;

function _shifted(deck: Deck): boolean {
	return deck === 2;
}

function _jog(deck: Deck, id: number): MidiBinding {
	return {
		source: cc(id),
		action: { type: 'deck_jog', deck },
		relative: true,
		relativeCentered: true,
		...(_shifted(deck) ? { shift: true } : {})
	};
}

function _mode(deck: Deck, noteId: number, mode: ControllerPadMode): MidiBinding {
	return {
		source: note(noteId),
		action: { type: 'controller_pad_mode', deck, mode },
		...(_shifted(deck) ? { shift: true } : {})
	};
}

function _sharedDeckBindings(deck: Deck): MidiBinding[] {
	return [
		// [Manual] PLAY: 9x 07.
		{
			source: note(WIRE.play),
			action: { type: 'deck_play_toggle', deck },
			...(_shifted(deck) ? { shift: true } : {})
		},
		// [Manual] CUE: 9x 08.
		{
			source: note(WIRE.cue),
			action: { type: 'deck_cue', deck },
			...(_shifted(deck) ? { shift: true } : {})
		},
		// [Manual] SYNC: 9x 06.
		{
			source: note(WIRE.sync),
			action: { type: 'deck_sync_toggle', deck },
			...(_shifted(deck) ? { shift: true } : {})
		},
		// [Manual] KEY3 "1": 9x 03.
		{
			source: note(WIRE.k1),
			action: { type: 'controller_pad', deck, pad: 1, shifted: false },
			...(_shifted(deck) ? { shift: true } : {})
		},
		// [Manual] KEY4 "2": 9x 04.
		{
			source: note(WIRE.k2),
			action: { type: 'controller_pad', deck, pad: 2, shifted: false },
			...(_shifted(deck) ? { shift: true } : {})
		},
		// [Manual] ENC2 LOAD push: 9x 0C.
		{
			source: note(WIRE.load),
			action: { type: 'browse_load', deck },
			...(_shifted(deck) ? { shift: true } : {})
		},
		// [Manual] MODE SAMPLER: 9x 11.
		_mode(deck, WIRE.modeSampler, 'sampler'),
		// [Manual] MODE LOOP: 9x 12.
		_mode(deck, WIRE.modeLoop, 'auto_loop'),
		// [Manual] MODE FX: 9x 13.
		_mode(deck, WIRE.modeFx, 'instant_fx'),
		// [Manual] MODE CUE/SCRATCH: 9x 14.
		_mode(deck, WIRE.modeCueScratch, 'hot_cue'),
		// [Manual] JOG: Bx 13, 0x40 ± NUM.
		_jog(deck, WIRE.jog),
		// [Manual] SCRATCH: Bx 15, same encoding.
		_jog(deck, WIRE.scratch)
	];
}

function _mixerBindings(deck: Deck): MidiBinding[] {
	const m = MIXER[deck];
	return [
		// [Manual] FADER A/B: Bx 21 / Bx 22.
		{ source: cc(m.fader), action: { type: 'mixer_channel', deck, target: 'fader' } },
		// [Manual] CHA/B GAIN: Bx 24 / Bx 25.
		{ source: cc(m.trim), action: { type: 'mixer_channel', deck, target: 'trim' } },
		// [Manual] CHA/B HI: Bx 26 / Bx 27 (ADC6 row printed twice; bound once).
		{
			source: cc(m.eqHigh),
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'high' }
		},
		// [Manual] CHA/B MID: Bx 28 / Bx 29.
		{
			source: cc(m.eqMid),
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'mid' }
		},
		// [Manual] CHA/B LOW: Bx 2A / Bx 2B.
		{
			source: cc(m.eqLow),
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'low' }
		}
	];
}

function _channelCueBindings(): MidiBinding[] {
	return [
		// [Manual] KEY1 CHA PLF: 9x 01.
		{ source: note(0x01), action: { type: 'channel_cue', deck: 1 } },
		// [Manual] KEY2 CHB PLF: 9x 02.
		{ source: note(0x02), action: { type: 'channel_cue', deck: 2 } }
	];
}

function _globalBindings(): MidiBinding[] {
	return [
		// [Manual] CROSS F: Bx 23 (CF CURVE shares this CC; crossfader only).
		{ source: cc(0x23), action: { type: 'mixer_global', target: 'crossfader' } },
		// [Manual] SW141 centre encoder: Bx 11. Mixxx cross-check: select-track on cc 17.
		{
			source: cc(0x11),
			action: { type: 'browse_encoder' },
			relative: true,
			relativeCentered: true
		}
	];
}

function _led(noteId: number, trigger: LedRule['trigger']): LedRule {
	return {
		trigger,
		out: { ch: CH, note: noteId, velocityOn: MIX101_LED_ON, velocityOff: MIX101_LED_OFF }
	};
}

function _ledRules(): LedRule[] {
	return [
		// [Manual] LED1 PFL A / LED11 CUE: channel cue + deck loaded (software choice).
		_led(0x01, { kind: 'channel_cue_enabled', deck: 1 }),
		_led(0x02, { kind: 'channel_cue_enabled', deck: 2 }),
		// [Manual] LED12 PLAY: 9x 07.
		_led(WIRE.play, { kind: 'deck_playing', deck: 1 }),
		// [Manual] LED11 CUE row shares note 0x08. Software choice: lit when loaded.
		_led(WIRE.cue, { kind: 'deck_loaded', deck: 1 }),
		// [Manual] LED5-8 MODE indicators.
		_led(WIRE.modeSampler, { kind: 'pad_mode_selected', deck: 1, mode: 'sampler' }),
		_led(WIRE.modeLoop, { kind: 'pad_mode_selected', deck: 1, mode: 'auto_loop' }),
		_led(WIRE.modeFx, { kind: 'pad_mode_selected', deck: 1, mode: 'instant_fx' }),
		_led(WIRE.modeCueScratch, { kind: 'pad_mode_selected', deck: 1, mode: 'hot_cue' })
	];
}

function _hints(): ControlHint[] {
	return [
		// [Manual] KEY5 SELECT: 9x 05.
		{ source: note(0x05), label: 'SELECT (KEY5)' },
		// [Manual] ENC1 "1" push: 9x 0B.
		{ source: note(0x0b), label: 'Encoder 1 push (ENC1 "1")' },
		// [Manual] ENC3 "2" push: 9x 0D.
		{ source: note(0x0d), label: 'Encoder 3 push (ENC3 "2")' },
		// [Manual] JOG TOUCH: 9x 15 (no touch action in contract).
		{ source: note(0x15), label: 'Jog touch' },
		// [Manual] EQ centre detent kills.
		{ source: note(0x50), label: 'EQ centre detent CHA HI' },
		{ source: note(0x51), label: 'EQ centre detent CHB HI' },
		{ source: note(0x52), label: 'EQ centre detent CHA MID' },
		{ source: note(0x53), label: 'EQ centre detent CHB MID' },
		{ source: note(0x54), label: 'EQ centre detent CHA LOW' },
		{ source: note(0x55), label: 'EQ centre detent CHB LOW' },
		// [Manual] MIDI OUT channel select: Bx 0E.
		{ source: cc(0x0e), label: 'MIDI OUT channel select' },
		// [Manual] SW142 / SW143 encoders.
		{ source: cc(0x10), label: 'Encoder SW142' },
		{ source: cc(0x12), label: 'Encoder SW143' }
	];
}

export const DJ_TECH_MIX_101_MAP: DeviceMap = {
	vendor: 'DJ-Tech',
	// Matches DJ-Tech Mix-101, DJ-Tech MIX-101, MIX-101, MIX-101 MIDI 1, Mix101.
	// regex_lite: no lookahead.
	nameMatch: 'MIX[\\s_-]*101',
	bindings: [
		// [Manual] KEY9 A/B: 9x 09.
		{ source: note(WIRE.abKey), action: { type: 'shift_modifier' } },
		..._channelCueBindings(),
		...DECKS.flatMap((deck) => _sharedDeckBindings(deck)),
		...DECKS.flatMap((deck) => _mixerBindings(deck)),
		..._globalBindings()
	],
	leds: _ledRules(),
	hints: _hints()
};

/** Self-registration: maps/entries.ts discovers this export, so no shared list names this map. */
export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{ key: 'DJ_TECH_MIX_101_MAP', map: DJ_TECH_MIX_101_MAP, live: true }
];
