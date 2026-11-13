/**
 * DJ-Tech i-Mix Reload live device map (catalog id dj-tech-i-mix-reload).
 *
 * SOURCE OF TRUTH for EVERY Note/CC number in this file: DJ-Tech i-Mix Reload
 * User Manual, English section MIDI MAP (top-down KEY/LED/VR/ENC plate plus the
 * Note/CTRL hex table that follows it). Public copies (not redistributed here):
 *   https://www.manualslib.com/manual/2125254/Dj-Tech-I-Mix-Reload.html
 *   https://manualzz.com/doc/5350298/dj-tech-i-mix-reload-dj-midi-controller-user-manual
 * Function names come from the plate legends (SYNC / CUE / play, GAIN, HIGH,
 * MID, LOW, PFL, LOAD A / LOAD B, FX DECK A / FX DECK B, PH MIX / PH VOL).
 *
 * NOT HARDWARE CONFIRMED. No hardware on this issue; fader polarity is left
 * uninverted because the chart prints no min/max orientation.
 *
 * Mixxx XML/JS and tools/deck-diagrams/devices/dj-tech-i-mix-reload/midi.json
 * were NOT used as wire sources.
 *
 * Channel: the chart prints no channel column, so every source is ch 1.
 * Deck 1 is the left platter (KEY39 / ENC2), deck 2 the right (KEY45 / ENC3).
 *
 * JOG: the chart encodes each platter as paired Note CW/CCW pulses (ENC2 touch
 * ON 0x60/0x61, ENC3 touch ON 0x62/0x63), but P0 deck_jog needs a relative CC.
 * The touch-ON pulse notes drive deck_tempo_nudge (the playing-jog behaviour);
 * the touch-OFF rows (0x70-0x77) are hints.
 *
 * BROWSE: ENC1 (SONG SELECT) is Note CW 0x64 / CCW 0x65, and browse_encoder
 * needs a relative CC, so ENC1 is hinted; LOAD A / LOAD B bind browse_load.
 *
 * FX: MidiAction has no beat/colour FX member. Each deck's FX X knob (VR1 deck
 * A, VR3 deck B) binds mixer_channel filter; FX Y knobs and FX keys are hints.
 *
 * Chart defects: KEY23 and KEY27 both print Note 0x12, so neither binds and
 * 0x12 is hinted once. KEY44 (deck 2 SET) and KEY45 (deck 2 platter touch) are
 * on the plate but have no hex row; they are absent here. CENTER CCs are
 * detents and stay hints.
 */

import type {
	ControlHint,
	DeviceMap,
	DeviceMapEntry,
	LedRule,
	MidiBinding
} from '$lib/rb/midi/midi-types';

type Deck = 1 | 2;

/** MIDI MAP wires that differ per deck. */
interface DeckWires {
	sync: number;
	cue: number;
	play: number;
	pfl: number;
	load: number;
	jogCw: number;
	jogCcw: number;
	fader: number;
	gain: number;
	high: number;
	mid: number;
	low: number;
	pitch: number;
	fx: number;
}

const _WIRES: Record<Deck, DeckWires> = {
	// [MIDI MAP] KEY13 SYNC 0x05, KEY14 CUE 0x0B, KEY15 play 0x11, KEY17 PFL 0x15,
	// KEY42 LOAD A 0x2E, ENC2 ON CW 0x60 / CCW 0x61, VR14 0x43, VR6 GAIN 0x47,
	// VR8 HIGH 0x4E, VR10 MID 0x4F, VR12 LOW 0x4D, VR5 pitch 0x45, VR1 FX X 0x44
	1: {
		sync: 0x05,
		cue: 0x0b,
		play: 0x11,
		pfl: 0x15,
		load: 0x2e,
		jogCw: 0x60,
		jogCcw: 0x61,
		fader: 0x43,
		gain: 0x47,
		high: 0x4e,
		mid: 0x4f,
		low: 0x4d,
		pitch: 0x45,
		fx: 0x44
	},
	// [MIDI MAP] KEY34 SYNC 0x23, KEY35 CUE 0x29, KEY36 play 0x2F, KEY18 PFL 0x1B,
	// KEY43 LOAD B 0x1A, ENC3 ON CW 0x62 / CCW 0x63, VR15 0x42, VR7 GAIN 0x51,
	// VR9 HIGH 0x50, VR11 MID 0x48, VR13 LOW 0x52, VR19 pitch 0x4B, VR3 FX X 0x46
	2: {
		sync: 0x23,
		cue: 0x29,
		play: 0x2f,
		pfl: 0x1b,
		load: 0x1a,
		jogCw: 0x62,
		jogCcw: 0x63,
		fader: 0x42,
		gain: 0x51,
		high: 0x50,
		mid: 0x48,
		low: 0x52,
		pitch: 0x4b,
		fx: 0x46
	}
};

/** Per-deck bindings, every id from the [MIDI MAP] row named in _WIRES. */
function _deckBindings(deck: Deck): MidiBinding[] {
	const w = _WIRES[deck];
	const note = (id: number) => ({ ch: 1, kind: 'note' as const, id });
	const cc = (id: number) => ({ ch: 1, kind: 'cc' as const, id });
	return [
		{ source: note(w.play), action: { type: 'deck_play_toggle', deck } },
		{ source: note(w.cue), action: { type: 'deck_cue', deck } },
		{ source: note(w.sync), action: { type: 'deck_sync_toggle', deck } },
		{ source: note(w.pfl), action: { type: 'channel_cue', deck } },
		{ source: note(w.load), action: { type: 'browse_load', deck } },
		{ source: note(w.jogCw), action: { type: 'deck_tempo_nudge', deck, direction: 1 } },
		{ source: note(w.jogCcw), action: { type: 'deck_tempo_nudge', deck, direction: -1 } },
		{ source: cc(w.fader), action: { type: 'mixer_channel', deck, target: 'fader' } },
		{ source: cc(w.gain), action: { type: 'mixer_channel', deck, target: 'trim' } },
		{ source: cc(w.high), action: { type: 'mixer_channel', deck, target: 'eq', band: 'high' } },
		{ source: cc(w.mid), action: { type: 'mixer_channel', deck, target: 'eq', band: 'mid' } },
		{ source: cc(w.low), action: { type: 'mixer_channel', deck, target: 'eq', band: 'low' } },
		{ source: cc(w.pitch), action: { type: 'deck_pitch', deck, lsbOffset: null } },
		{ source: cc(w.fx), action: { type: 'mixer_channel', deck, target: 'filter' } }
	];
}

/** Global bindings: [MIDI MAP] VR16 crossfader 0x41, VR17 PH MIX 0x4A, VR18 PH VOL 0x49. */
const _GLOBAL_BINDINGS: MidiBinding[] = [
	{
		source: { ch: 1, kind: 'cc', id: 0x41 },
		action: { type: 'mixer_global', target: 'crossfader' }
	},
	{ source: { ch: 1, kind: 'cc', id: 0x4a }, action: { type: 'headphone_mix' } },
	{ source: { ch: 1, kind: 'cc', id: 0x49 }, action: { type: 'headphone_level' } }
];

/** Chart wires left unbound, each labelled from the [MIDI MAP] plate. */
const _HINT_ROWS: ReadonlyArray<readonly ['note' | 'cc', number, string]> = [
	['note', 0x04, 'FX deck A down (KEY1)'],
	['note', 0x0a, 'FX deck A up (KEY2)'],
	['note', 0x10, 'FX deck B down (KEY3)'],
	['note', 0x16, 'FX deck B up (KEY4)'],
	['note', 0x08, '1 (deck 1, KEY5)'],
	['note', 0x0e, '2 (deck 1, KEY6)'],
	['note', 0x14, '3 (deck 1, KEY7)'],
	['note', 0x01, 'LOOP (deck 1, KEY8)'],
	['note', 0x07, 'Loop size - (deck 1, KEY9)'],
	['note', 0x0d, 'Loop size + (deck 1, KEY10)'],
	['note', 0x13, 'RELOOPER (deck 1, KEY11)'],
	['note', 0x0c, 'SEARCH (deck 1, KEY12)'],
	['note', 0x17, 'DOWNBEAT (deck 1, KEY16)'],
	['note', 0x1c, 'HIGH KILL (deck 1, KEY19)'],
	['note', 0x22, 'HIGH KILL (deck 2, KEY20)'],
	['note', 0x18, 'MID KILL (deck 1, KEY21)'],
	['note', 0x1e, 'MID KILL (deck 2, KEY22)'],
	['note', 0x12, 'LOW KILL deck 1 (KEY23) or 3 deck 2 (KEY27): chart prints 0x12 twice'],
	['note', 0x24, 'LOW KILL (deck 2, KEY24)'],
	['note', 0x20, '1 (deck 2, KEY25)'],
	['note', 0x26, '2 (deck 2, KEY26)'],
	['note', 0x19, 'LOOP (deck 2, KEY28)'],
	['note', 0x1f, 'Loop size - (deck 2, KEY29)'],
	['note', 0x25, 'Loop size + (deck 2, KEY30)'],
	['note', 0x2b, 'RELOOPER (deck 2, KEY31)'],
	['note', 0x2a, 'SEARCH (deck 2, KEY32)'],
	['note', 0x1d, 'DOWNBEAT (deck 2, KEY33)'],
	['note', 0x03, 'KEY LOCK (deck 1, KEY37)'],
	['note', 0x02, 'SET (deck 1, KEY38)'],
	['note', 0x21, 'Jog touch (deck 1, KEY39)'],
	['note', 0x2d, 'BROWSER (KEY40)'],
	['note', 0x28, 'KEY LOCK (deck 2, KEY41)'],
	['note', 0x27, 'SONG SELECT push (ENC1_KEY)'],
	['note', 0x64, 'SONG SELECT CW (ENC1)'],
	['note', 0x65, 'SONG SELECT CCW (ENC1)'],
	['note', 0x70, 'Jog touch-off CW (deck 1, ENC2 SEARCH ON)'],
	['note', 0x71, 'Jog touch-off CCW (deck 1, ENC2 SEARCH ON)'],
	['note', 0x72, 'Jog touch-off CW (deck 1, ENC2 SEARCH OFF)'],
	['note', 0x73, 'Jog touch-off CCW (deck 1, ENC2 SEARCH OFF)'],
	['note', 0x74, 'Jog touch-off CW (deck 2, ENC3 SEARCH ON)'],
	['note', 0x75, 'Jog touch-off CCW (deck 2, ENC3 SEARCH ON)'],
	['note', 0x76, 'Jog touch-off CW (deck 2, ENC3 SEARCH OFF)'],
	['note', 0x77, 'Jog touch-off CCW (deck 2, ENC3 SEARCH OFF)'],
	['cc', 0x40, 'FX Y (deck A, VR2)'],
	['cc', 0x4c, 'FX Y (deck B, VR4)'],
	['cc', 0x66, 'Pitch center (deck 1, VR5 CENTER)'],
	['cc', 0x67, 'Pitch center (deck 2, VR19 CENTER)'],
	['cc', 0x68, 'HIGH center (deck 1, VR8 CENTER)'],
	['cc', 0x6b, 'HIGH center (deck 2, VR9 CENTER)'],
	['cc', 0x69, 'MID center (deck 1, VR10 CENTER)'],
	['cc', 0x6c, 'MID center (deck 2, VR11 CENTER)'],
	['cc', 0x6a, 'LOW center (deck 1, VR12 CENTER)'],
	['cc', 0x6d, 'LOW center (deck 2, VR13 CENTER)']
];

const _HINTS: ControlHint[] = _HINT_ROWS.map(([kind, id, label]) => ({
	source: { ch: 1, kind, id },
	label
}));

/** LED rules where the [MIDI MAP] prints LED n on the same note as KEY n. */
function _deckLeds(deck: Deck): LedRule[] {
	const w = _WIRES[deck];
	const out = (note: number) => ({ ch: 1, note, velocityOn: 0x7f, velocityOff: 0x00 });
	return [
		{ trigger: { kind: 'deck_playing', deck }, out: out(w.play) },
		{ trigger: { kind: 'beat_sync_enabled', deck }, out: out(w.sync) },
		{ trigger: { kind: 'channel_cue_enabled', deck }, out: out(w.pfl) }
	];
}

/** Catalog USB product `DJ-Tech i-Mix Reload` plus port suffixes; regex_lite-safe
 * (no lookaround), refuses i-Mix Reload MKII / MK-II and the i-Mix Club Edition. */
const _NAME_MATCH = '(?:DJ-Tech\\s+)?i-Mix\\s+Reload(?:$|\\s+MIDI|\\s+\\d)';

/** DJ-Tech i-Mix Reload live P0 map, wires from the User Manual MIDI MAP. */
export const DJ_TECH_I_MIX_RELOAD_MAP: DeviceMap = {
	vendor: 'DJ-Tech',
	nameMatch: _NAME_MATCH,
	bindings: [..._deckBindings(1), ..._deckBindings(2), ..._GLOBAL_BINDINGS],
	leds: [..._deckLeds(1), ..._deckLeds(2)],
	hints: _HINTS
};

/** Self-registration: maps/entries.ts discovers this export, so no shared list names this map. */
export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{ key: 'DJ_TECH_I_MIX_RELOAD_MAP', map: DJ_TECH_I_MIX_RELOAD_MAP, live: true }
];
