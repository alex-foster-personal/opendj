/**
 * Hercules DJ Console RMX 2 live device map (catalog id hercules-dj-console-rmx-2).
 *
 * SOURCE OF TRUTH for EVERY Note/CC number in this file: Hercules
 * "Appendix – DJConsole RMX2 MIDI Controls" (RMX2_MIDI_Commands.pdf),
 * https://ts.hercules.com/download/sound/manuals/Rmx2/RMX2_MIDI_Commands.pdf
 * Sections: **I Midi In** (host-bound wires), **II Midi Out**
 * (`UpdateAllControls` B0 7F xx), **LEDs control on Rmx2** (same note as
 * MIDI-IN, 00 off / 7F on). Chart notes: values are hex; `x` is the MIDI
 * channel nibble (`0` = channel 1); `9x` Note On/Off, `Bx` Control Change.
 * Product / USB names on the sheet: DJConsole RMX2 / DJConsole Rmx2.
 *
 * hardware_confirmed: false. No hardware on this issue; polarity of pitch
 * and mixer rows is taken from the sheet ("Full Down > Full Up",
 * "Full Left > Full Right") and left uninverted until a capture confirms.
 *
 * Channel layout: every control is on ONE MIDI channel (default 1). Decks
 * are distinguished by note/CC id, not by channel. Shift is hardware-encoded
 * (shifted pads send `Bx` instead of `9x`; shifted transport uses different
 * notes), so this map does not bind `shift_modifier`.
 *
 * DO NOT source numbers from tools/deck-diagrams/devices/hercules-dj-console-rmx-2/midi.json
 * or from Mixxx: those are GPL Mixxx dumps and this repo is Apache 2.0.
 * Mixxx CCs disagree with the chart (Mixxx volume CC 68 / crossfader CC 72
 * vs chart `Bx 3A` / `Bx 48`). Play/cue/sync notes 0x21/0x22/0x23 for deck A
 * happen to match; they are still taken from the PDF.
 *
 * FX pads bind as `controller_pad` because MidiAction has no beat/colour FX
 * member. Colour-FX analog on this contract is `mixer_channel` `filter`; this
 * device has no filter knob, so none is invented. Default pad mode is
 * `hot_cue`, so an FX-pad press currently also hits the pad runtime's hot-cue
 * path until `instant_fx` is implemented (P0 gap; do not widen MidiAction).
 * Cue pads bind as `deck_hot_cue` A-D (direct). Loop pads bind as
 * `deck_beat_loop` (direct, software beat ladder).
 *
 * Jog CCs are "Incremental – Coarse" centred on 0x40 (`7F > 40` CCW,
 * `01 > 3F` CW): `relative` + `relativeCentered`. Pitch LSB is the next CC
 * (`Bx 36`/`Bx 37`, `Bx 38`/`Bx 39`), so `lsbOffset: 1`. Mixer 14-bit caveat:
 * bind MSB only; LSB CCs are hints. `deck_pitch` is the only 14-bit pairing.
 * No meters: chart meters are MIDI-OUT Note LEDs (9x 49-4E / 9x 4F-54), and
 * MidiMeterOutput is CC-only. Those LED notes are not hinted: 9x 52-54 are
 * already inbound Shift Rewind/Forward/Pitch Bend Minus on deck A.
 */

import type {
	ControlHint,
	DeviceMap,
	DeviceMapEntry,
	LedRule,
	MidiBinding,
	MidiSource
} from '$lib/rb/midi/midi-types';
import type { HotCueSlot } from '$lib/rb/hot-cue-types';

const CH = 1; // PDF: x=0 → MIDI channel 1
const DECKS = [1, 2] as const;
type Deck = (typeof DECKS)[number];
const PADS = [1, 2, 3, 4] as const;
const HOT_CUE_SLOTS: readonly HotCueSlot[] = ['A', 'B', 'C', 'D'];

const note = (id: number): MidiSource => ({ ch: CH, kind: 'note', id });
const cc = (id: number): MidiSource => ({ ch: CH, kind: 'cc', id });

/** Beats per Loop Pad 1..4. The sheet documents only the wires; the ladder
 * is our software assignment, the same house rule as DDJ_REV1_AUTO_LOOP_PAD_BEATS. */
export const RMX2_LOOP_PAD_BEATS: readonly number[] = [1, 2, 4, 8];
/** [PDF] LEDs control on Rmx2: 7F on. */
export const RMX2_LED_ON = 0x7f;
/** [PDF] LEDs control on Rmx2: 00 off. */
export const RMX2_LED_OFF = 0x00;

/** Per-deck note/CC ids from [PDF] I Midi In. Deck A = 1, Deck B = 2. */
const WIRE: Record<
	Deck,
	{
		play: number;
		cue: number;
		sync: number;
		load: number;
		pitchMinus: number;
		pitchPlus: number;
		jog: number;
		jogScratch: number;
		pitchMsb: number;
		volume: number;
		treble: number;
		medium: number;
		bass: number;
		gain: number;
		fxPad0: number;
		cuePad0: number;
		loopPad0: number;
		samplePad0: number;
		jogPress: number;
		rewind: number;
		forward: number;
		shiftRewind: number;
		shiftForward: number;
		killTreble: number;
		killMid: number;
		killBass: number;
		source: number;
		preview: number;
		shiftPitchMinus: number;
		shiftPitchPlus: number;
		loopMode: number;
		fxMode: number;
		sampleMode: number;
		cueMode: number;
		shiftLoopMode: number;
		shiftFxMode: number;
		shiftSampleMode: number;
		shiftCueMode: number;
		shiftPadCc0: number;
	}
> = {
	1: {
		play: 0x21,
		cue: 0x22,
		sync: 0x23,
		load: 0x24,
		pitchMinus: 0x2c,
		pitchPlus: 0x2d,
		jog: 0x30,
		jogScratch: 0x32,
		pitchMsb: 0x36,
		volume: 0x3a,
		treble: 0x3c,
		medium: 0x3e,
		bass: 0x40,
		gain: 0x42,
		fxPad0: 0x01,
		cuePad0: 0x09,
		loopPad0: 0x0d,
		samplePad0: 0x05,
		jogPress: 0x2f,
		rewind: 0x26,
		forward: 0x27,
		shiftRewind: 0x52,
		shiftForward: 0x53,
		killTreble: 0x28,
		killMid: 0x29,
		killBass: 0x2a,
		source: 0x2b,
		preview: 0x2e,
		shiftPitchMinus: 0x54,
		shiftPitchPlus: 0x55,
		loopMode: 0x34,
		fxMode: 0x54,
		sampleMode: 0x56,
		cueMode: 0x58,
		shiftLoopMode: 0x5a,
		shiftFxMode: 0x5c,
		shiftSampleMode: 0x5e,
		shiftCueMode: 0x60,
		shiftPadCc0: 0x01
	},
	2: {
		play: 0x32,
		cue: 0x33,
		sync: 0x34,
		load: 0x35,
		pitchMinus: 0x3d,
		pitchPlus: 0x3e,
		jog: 0x31,
		jogScratch: 0x33,
		pitchMsb: 0x38,
		volume: 0x4a,
		treble: 0x4c,
		medium: 0x4e,
		bass: 0x50,
		gain: 0x52,
		fxPad0: 0x11,
		cuePad0: 0x19,
		loopPad0: 0x1d,
		samplePad0: 0x15,
		jogPress: 0x40,
		rewind: 0x37,
		forward: 0x38,
		shiftRewind: 0x56,
		shiftForward: 0x57,
		killTreble: 0x39,
		killMid: 0x3a,
		killBass: 0x3b,
		source: 0x3c,
		preview: 0x3f,
		shiftPitchMinus: 0x58,
		shiftPitchPlus: 0x59,
		loopMode: 0x35,
		fxMode: 0x55,
		sampleMode: 0x57,
		cueMode: 0x59,
		shiftLoopMode: 0x5b,
		shiftFxMode: 0x5d,
		shiftSampleMode: 0x5f,
		shiftCueMode: 0x61,
		shiftPadCc0: 0x11
	}
};

function _jog(deck: Deck, id: number): MidiBinding {
	return {
		source: cc(id),
		action: { type: 'deck_jog', deck },
		relative: true,
		relativeCentered: true
	};
}

function _deckBindings(deck: Deck): MidiBinding[] {
	const w = WIRE[deck];
	return [
		// [PDF] Play Deck A/B: 9x 21 / 9x 32.
		{ source: note(w.play), action: { type: 'deck_play_toggle', deck } },
		// [PDF] Cue Deck A/B: 9x 22 / 9x 33.
		{ source: note(w.cue), action: { type: 'deck_cue', deck } },
		// [PDF] Sync Deck A/B: 9x 23 / 9x 34.
		{ source: note(w.sync), action: { type: 'deck_sync_toggle', deck } },
		// [PDF] Load Deck A/B: 9x 24 / 9x 35.
		{ source: note(w.load), action: { type: 'browse_load', deck } },
		// [PDF] Pitch Bend Minus/Plus Deck A: 9x 2C / 9x 2D; Deck B: 9x 3D / 9x 3E.
		{ source: note(w.pitchMinus), action: { type: 'deck_tempo_nudge', deck, direction: -1 } },
		{ source: note(w.pitchPlus), action: { type: 'deck_tempo_nudge', deck, direction: 1 } },
		// [PDF] Jog Deck A/B: Bx 30 / Bx 31 Incremental – Coarse, centred on 0x40.
		_jog(deck, w.jog),
		// [PDF] Jog Scratch Deck A/B: Bx 32 / Bx 33, same incremental encoding.
		_jog(deck, w.jogScratch),
		// [PDF] Pitch fader Deck A MSB/LSB: Bx 36 / Bx 37; Deck B: Bx 38 / Bx 39.
		{ source: cc(w.pitchMsb), action: { type: 'deck_pitch', deck, lsbOffset: 1 } }
	];
}

function _mixerBindings(deck: Deck): MidiBinding[] {
	const w = WIRE[deck];
	return [
		// [PDF] Volume fader Deck A/B MSB: Bx 3A / Bx 4A, Full Down > Full Up.
		{ source: cc(w.volume), action: { type: 'mixer_channel', deck, target: 'fader' } },
		// [PDF] Treble Deck A/B MSB: Bx 3C / Bx 4C.
		{
			source: cc(w.treble),
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'high' }
		},
		// [PDF] Medium Deck A/B MSB: Bx 3E / Bx 4E.
		{
			source: cc(w.medium),
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'mid' }
		},
		// [PDF] Bass Deck A/B MSB: Bx 40 / Bx 50.
		{
			source: cc(w.bass),
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'low' }
		},
		// [PDF] Gain Deck A/B MSB: Bx 42 / Bx 52.
		{ source: cc(w.gain), action: { type: 'mixer_channel', deck, target: 'trim' } }
	];
}

function _padBindings(deck: Deck): MidiBinding[] {
	const w = WIRE[deck];
	const out: MidiBinding[] = [];
	PADS.forEach((pad, i) => {
		const slot = HOT_CUE_SLOTS[i];
		const beats = RMX2_LOOP_PAD_BEATS[i];
		if (slot === undefined || beats === undefined) return;
		// [PDF] FX Pad 1-4 Deck A: 9x 01-04; Deck B: 9x 11-14.
		out.push({
			source: note(w.fxPad0 + i),
			action: { type: 'controller_pad', deck, pad, shifted: false }
		});
		// [PDF] Cue Pad 1-4 Deck A: 9x 09-0C; Deck B: 9x 19-1C.
		out.push({
			source: note(w.cuePad0 + i),
			action: { type: 'deck_hot_cue', deck, slot }
		});
		// [PDF] Loop Pad 1-4 Deck A: 9x 0D-10; Deck B: 9x 1D-20.
		out.push({
			source: note(w.loopPad0 + i),
			action: { type: 'deck_beat_loop', deck, beats }
		});
	});
	return out;
}

function _globalBindings(): MidiBinding[] {
	return [
		// [PDF] Main Volume MSB: Bx 44.
		{ source: cc(0x44), action: { type: 'mixer_global', target: 'master' } },
		// [PDF] Cue to Mix MSB: Bx 46. The sheet has Cue to Mix only, no headphone level.
		{ source: cc(0x46), action: { type: 'headphone_mix' } },
		// [PDF] Cross fader MSB: Bx 48, Full Left > Full Right.
		{ source: cc(0x48), action: { type: 'mixer_global', target: 'crossfader' } }
	];
}

function _led(noteId: number, trigger: LedRule['trigger']): LedRule {
	return {
		trigger,
		out: { ch: CH, note: noteId, velocityOn: RMX2_LED_ON, velocityOff: RMX2_LED_OFF }
	};
}

function _ledRules(deck: Deck): LedRule[] {
	const w = WIRE[deck];
	return [
		// [PDF] LEDs control on Rmx2: Play Deck A/B 9x 21 / 9x 32.
		_led(w.play, { kind: 'deck_playing', deck }),
		// [PDF] Cue Deck A/B 9x 22 / 9x 33. Software choice (ours): lit when loaded.
		_led(w.cue, { kind: 'deck_loaded', deck }),
		// [PDF] Sync Deck A/B 9x 23 / 9x 34.
		_led(w.sync, { kind: 'beat_sync_enabled', deck }),
		// [PDF] Cue Pad 1-4 Deck A 9x 09-0C / Deck B 9x 19-1C.
		...HOT_CUE_SLOTS.map((slot, i) => _led(w.cuePad0 + i, { kind: 'hot_cue_present', deck, slot })),
		// [PDF] Loop Pad 1-4 Deck A 9x 0D-10 / Deck B 9x 1D-20.
		...RMX2_LOOP_PAD_BEATS.map((beats, i) =>
			_led(w.loopPad0 + i, { kind: 'loop_beats_engaged', deck, beats })
		)
	];
}

function _deckHints(deck: Deck): ControlHint[] {
	const w = WIRE[deck];
	const d = `(deck ${deck})`;
	const hints: ControlHint[] = [
		// [PDF] Sample Pad 1-4 Deck A 9x 05-08 / Deck B 9x 15-18.
		...PADS.map((pad, i) => ({
			source: note(w.samplePad0 + i),
			label: `Sample Pad ${pad} ${d}`
		})),
		// [PDF] Shift+FX/Sample/Cue/Loop pads send Bx with the same id as the unshifted note.
		...PADS.map((pad, i) => ({
			source: cc(w.shiftPadCc0 + i),
			label: `Shift + FX Pad ${pad} ${d}`
		})),
		...PADS.map((pad, i) => ({
			source: cc(w.shiftPadCc0 + 4 + i),
			label: `Shift + Sample Pad ${pad} ${d}`
		})),
		...PADS.map((pad, i) => ({
			source: cc(w.shiftPadCc0 + 8 + i),
			label: `Shift + Cue Pad ${pad} ${d}`
		})),
		...PADS.map((pad, i) => ({
			source: cc(w.shiftPadCc0 + 12 + i),
			label: `Shift + Loop Pad ${pad} ${d}`
		})),
		// [PDF] Jog Wheel Pressed Down Deck A/B: 9x 2F / 9x 40.
		{ source: note(w.jogPress), label: `Jog Wheel Pressed Down ${d}` },
		// [PDF] Rewind/Forward Deck A: 9x 26 / 9x 27; Deck B: 9x 37 / 9x 38.
		{ source: note(w.rewind), label: `Rewind ${d}` },
		{ source: note(w.forward), label: `Forward ${d}` },
		// [PDF] Shift Rewind/Forward Deck A: 9x 52 / 9x 53; Deck B: 9x 56 / 9x 57.
		{ source: note(w.shiftRewind), label: `Shift Rewind ${d}` },
		{ source: note(w.shiftForward), label: `Shift Forward ${d}` },
		// [PDF] Kill Treble/Medium/Bass Deck A: 9x 28 / 9x 29 / 9x 2A; Deck B: 9x 39 / 9x 3A / 9x 3B.
		{ source: note(w.killTreble), label: `Kill Treble ${d}` },
		{ source: note(w.killMid), label: `Kill Medium ${d}` },
		{ source: note(w.killBass), label: `Kill Bass ${d}` },
		// [PDF] Source Deck A/B: 9x 2B / 9x 3C.
		{ source: note(w.source), label: `Source ${d}` },
		// [PDF] Preview Deck A/B: 9x 2E / 9x 3F.
		{ source: note(w.preview), label: `Preview ${d}` },
		// [PDF] Shift + Pitch Bend Minus/Plus Deck A: 9x 54 / 9x 55; Deck B: 9x 58 / 9x 59.
		{ source: note(w.shiftPitchMinus), label: `Shift Pitch Bend Minus ${d}` },
		{ source: note(w.shiftPitchPlus), label: `Shift Pitch Bend Plus ${d}` },
		// [PDF] Loop/Fx/Sample/Cue Mode encoders and Shift+ twins (Incremental – Coarse).
		{ source: cc(w.loopMode), label: `Loop Mode ${d}` },
		{ source: cc(w.fxMode), label: `Fx Mode ${d}` },
		{ source: cc(w.sampleMode), label: `Sample Mode ${d}` },
		{ source: cc(w.cueMode), label: `Cue Mode ${d}` },
		{ source: cc(w.shiftLoopMode), label: `Shift + Loop Mode ${d}` },
		{ source: cc(w.shiftFxMode), label: `Shift + Fx Mode ${d}` },
		{ source: cc(w.shiftSampleMode), label: `Shift + Sample Mode ${d}` },
		{ source: cc(w.shiftCueMode), label: `Shift + Cue Mode ${d}` },
		// [PDF] mixer LSB CCs (MSB bound; pitch LSB is paired via lsbOffset, not hinted).
		{ source: cc(w.volume + 1), label: `Volume fader LSB ${d}` },
		{ source: cc(w.treble + 1), label: `Treble LSB ${d}` },
		{ source: cc(w.medium + 1), label: `Medium LSB ${d}` },
		{ source: cc(w.bass + 1), label: `Bass LSB ${d}` },
		{ source: cc(w.gain + 1), label: `Gain LSB ${d}` }
	];
	return hints;
}

function _globalHints(): ControlHint[] {
	return [
		// [PDF] Files / Folders / Up / Down: 9x 43 / 9x 44 / 9x 45 / 9x 46.
		{ source: note(0x43), label: 'Files' },
		{ source: note(0x44), label: 'Folders' },
		{ source: note(0x45), label: 'Up' },
		{ source: note(0x46), label: 'Down' },
		// [PDF] Scratch: 9x 47.
		{ source: note(0x47), label: 'Scratch' },
		// [PDF] Microphone Talk over: 9x 48.
		{ source: note(0x48), label: 'Microphone Talk over' },
		// [PDF] Main Volume LSB: Bx 45. Cue to Mix LSB: Bx 47. Cross fader LSB: Bx 49.
		{ source: cc(0x45), label: 'Main Volume LSB' },
		{ source: cc(0x47), label: 'Cue to Mix LSB' },
		{ source: cc(0x49), label: 'Cross fader LSB' }
	];
}

export const HERCULES_DJ_CONSOLE_RMX_2_MAP: DeviceMap = {
	vendor: 'Hercules',
	// Matches DJConsole RMX2, DJConsole Rmx2, Hercules DJ Console RMX 2,
	// Hercules DJ Console RMX2 MIDI. Requires the trailing 2 so the original
	// RMX and RMX Advanced do not match. regex_lite: no lookahead.
	nameMatch: 'DJ\\s*Console\\s*RMX\\s*2',
	bindings: [
		...DECKS.flatMap((deck) => _deckBindings(deck)),
		...DECKS.flatMap((deck) => _mixerBindings(deck)),
		...DECKS.flatMap((deck) => _padBindings(deck)),
		..._globalBindings()
	],
	leds: DECKS.flatMap((deck) => _ledRules(deck)),
	hints: [...DECKS.flatMap((deck) => _deckHints(deck)), ..._globalHints()],
	// [PDF] II Midi Out UpdateAllControls: B0 7F xx — any value dumps analog positions.
	init: [[0xb0, 0x7f, 0x7f]]
};

/** Self-registration: maps/entries.ts discovers this export, so no shared list names this map. */
export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{ key: 'HERCULES_DJ_CONSOLE_RMX_2_MAP', map: HERCULES_DJ_CONSOLE_RMX_2_MAP, live: true }
];
