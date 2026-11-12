/**
 * Hercules DJ Control MP3 device map (original unit; not e2, LE or Glow).
 *
 * SOURCE OF TRUTH: "DJ Control MP3 - User Manual" (English),
 * https://ts.hercules.com/download/sound/manuals/DJ_Control_MP3/DJ_Control_MP3_Manual_English.pdf
 * section 6 "MIDI STANDARD SUPPORT", pages 14-15, table "MIDI input commands".
 * sha256 b51c390066cc3b54b697047320fcbcfc2049c39f4e383f2f655ee00454bd1e10.
 * Research note: docs/controller/research/Hercules-DJ-Control-MP3-sources.md.
 *
 * Every chart row is Control Change status B0 on MIDI channel 1 (43 rows total).
 *
 * Bound: per deck play, cue, sync (Auto Beat Select), PFL, jog, fader, 3-band EQ;
 *   global crossfader.
 * Hints: prev/next track, pitch bend +/-, master tempo, mouse buttons, 4 FX
 *   controls per deck, pitch knob, ministick X/Y; browse is not driven (no load
 *   wire and no relative browse encoder on the chart).
 *
 * ccButton on every button CC (ids 1..28): the device sends 7F press / 00 release.
 * Sync binds Auto Beat Select (no SYNC button on this hardware). Jog uses
 * relative: true only (incremental 01..3F / 7F..40). Master Tempo is key lock,
 * not deck_key_sync_toggle. Pitch knob and pitch bend are hints (incremental or
 * held bend vs absolute deck_pitch / permanent deck_tempo_nudge). FX wires are
 * hints only: MidiAction has no beat-FX or colour-FX action pending contract work.
 *
 * The chart documents MIDI-IN only (no MIDI-OUT rows), so there are no LED rules.
 *
 * NOT HARDWARE CONFIRMED.
 */

import type { ControlHint, DeviceMap, DeviceMapEntry, MidiBinding } from '$lib/rb/midi/midi-types';

type DeckId = 1 | 2;

interface DeckWireIds {
	play: number;
	cue: number;
	sync: number;
	pfl: number;
	jog: number;
	fader: number;
	eqLow: number;
	eqMid: number;
	eqHigh: number;
	prevTrack: number;
	nextTrack: number;
	pitchBendDown: number;
	pitchBendUp: number;
	masterTempo: number;
	mouseButton: number;
	fxSelect: number;
	fx1: number;
	fx2: number;
	fx3: number;
	pitchKnob: number;
}

const DECK_WIRES: Record<DeckId, DeckWireIds> = {
	1: {
		play: 0x08,
		cue: 0x09,
		sync: 0x0a,
		pfl: 0x15,
		jog: 0x36,
		fader: 0x32,
		eqLow: 0x2e,
		eqMid: 0x2f,
		eqHigh: 0x30,
		prevTrack: 0x0b,
		nextTrack: 0x0c,
		pitchBendDown: 0x14,
		pitchBendUp: 0x13,
		masterTempo: 0x16,
		mouseButton: 0x1b,
		fxSelect: 0x07,
		fx1: 0x0f,
		fx2: 0x0e,
		fx3: 0x0d,
		pitchKnob: 0x34
	},
	2: {
		play: 0x02,
		cue: 0x03,
		sync: 0x04,
		pfl: 0x19,
		jog: 0x37,
		fader: 0x33,
		eqLow: 0x2b,
		eqMid: 0x2c,
		eqHigh: 0x2d,
		prevTrack: 0x05,
		nextTrack: 0x06,
		pitchBendDown: 0x18,
		pitchBendUp: 0x17,
		masterTempo: 0x1a,
		mouseButton: 0x1c,
		fxSelect: 0x01,
		fx1: 0x10,
		fx2: 0x11,
		fx3: 0x12,
		pitchKnob: 0x35
	}
};

const CHART_CH = 1;

function cc(id: number): { ch: number; kind: 'cc'; id: number } {
	return { ch: CHART_CH, kind: 'cc', id };
}

function _deckBindings(deck: DeckId): MidiBinding[] {
	const w = DECK_WIRES[deck];
	return [
		// [PDF] §6 Play/Pause
		{ source: cc(w.play), action: { type: 'deck_play_toggle', deck }, ccButton: true },
		// [PDF] §6 Cue
		{ source: cc(w.cue), action: { type: 'deck_cue', deck }, ccButton: true },
		// [PDF] §6 Auto Beat Select
		{ source: cc(w.sync), action: { type: 'deck_sync_toggle', deck }, ccButton: true },
		// [PDF] §6 Headphone monitoring
		{ source: cc(w.pfl), action: { type: 'channel_cue', deck }, ccButton: true },
		// [PDF] §6 Jog Wheel (incremental)
		{ source: cc(w.jog), action: { type: 'deck_jog', deck }, relative: true },
		// [PDF] §6 Volume Slider
		{ source: cc(w.fader), action: { type: 'mixer_channel', deck, target: 'fader' } },
		// [PDF] §6 Bass Knob ("Base")
		{
			source: cc(w.eqLow),
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'low' }
		},
		// [PDF] §6 Medium Knob
		{
			source: cc(w.eqMid),
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'mid' }
		},
		// [PDF] §6 Treble Knob
		{
			source: cc(w.eqHigh),
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'high' }
		}
	];
}

function _deckHints(deck: DeckId): ControlHint[] {
	const w = DECK_WIRES[deck];
	return [
		{ source: cc(w.prevTrack), label: `Prev Track (deck ${deck})` },
		{ source: cc(w.nextTrack), label: `Next Track (deck ${deck})` },
		{ source: cc(w.pitchBendDown), label: `Pitch Bend "-" (deck ${deck})` },
		{ source: cc(w.pitchBendUp), label: `Pitch Bend "+" (deck ${deck})` },
		{ source: cc(w.masterTempo), label: `Master Tempo (deck ${deck})` },
		{ source: cc(w.mouseButton), label: `Mouse Button (deck ${deck})` },
		{ source: cc(w.fxSelect), label: `FX Select (deck ${deck})` },
		{ source: cc(w.fx1), label: `FX "1" (deck ${deck})` },
		{ source: cc(w.fx2), label: `FX "2" (deck ${deck})` },
		{ source: cc(w.fx3), label: `FX "3" (deck ${deck})` },
		{ source: cc(w.pitchKnob), label: `Pitch Knob, incremental (deck ${deck})` }
	];
}

const _GLOBAL_BINDINGS: MidiBinding[] = [
	// [PDF] §6 Cross Fader (00 left, 7F right)
	{ source: cc(0x31), action: { type: 'mixer_global', target: 'crossfader' } }
];

const _GLOBAL_HINTS: ControlHint[] = [
	{ source: cc(0x38), label: 'Ministick X-Axis' },
	{ source: cc(0x39), label: 'Ministick Y-Axis' }
];

export const HERCULES_DJ_CONTROL_MP3_MAP: DeviceMap = {
	vendor: 'Hercules',
	nameMatch: 'DJ Control MP3 MIDI',
	bindings: [..._deckBindings(1), ..._deckBindings(2), ..._GLOBAL_BINDINGS],
	leds: [],
	hints: [..._deckHints(1), ..._deckHints(2), ..._GLOBAL_HINTS]
};

/** Self-registration: maps/entries.ts discovers this export, so no shared list names this map. */
export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{ key: 'HERCULES_DJ_CONTROL_MP3_MAP', map: HERCULES_DJ_CONTROL_MP3_MAP, live: true }
];
