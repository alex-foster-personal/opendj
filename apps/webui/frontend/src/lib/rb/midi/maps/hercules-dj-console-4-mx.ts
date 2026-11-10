/**
 * Hercules DJ Console 4-Mx live device map (issue #10831).
 *
 * SOURCE OF TRUTH for every Note/CC number in this file: the official English
 * user manual appendix, not redistributed:
 *   https://ts.hercules.com/download/sound/manuals/4Mx/01_Eng_DJ_Console_4Mx.pdf
 * Cited below as [PDF]:
 *   Chapter 10 "CONFIGURATION IN MIDI MODE" (User Manual 27/30): select the
 *   device as DJ Console 4-Mx. Advanced tab default MIDI channels 1-2 (first
 *   deck family = x, second = y).
 *   APPENDIX: HERCULES DJ CONSOLE 4-MX MIDI CONTROLS
 *     Appendix 1/12–6/12: Virtual MIDI In (host receives).
 *     Appendix 7/12–12/12: Virtual MIDI Out (host sends LEDs / mode).
 *
 * Hex convention printed in the appendix:
 *   9x nn = Note On/Off, channel x (first MIDI channel).
 *   9y nn = Note On/Off, channel y (second MIDI channel).
 *   Bx nn = Control Change on channel x.
 *   x/y are 0-based nibbles. Default x=0 → Open DJ ch: 1; y=1 → ch: 2.
 *   Buttons 7F pressed / 00 released.
 *   Incremental (jog, FX): 7F > 40 CCW slow→fast, 01 > 3F CW slow→fast
 *   → relative, two's complement (0x01 = +1, 0x7F = -1; decodeRelative), not relativeCentered.
 *   Analog faders 00 > 7F Full Down → Full Up (volume, gain, pitch). No invert.
 *
 * Channel layout (not 1-deck-per-channel):
 *   Deck A notes 9x (ch 1); analog/jog/FX CCs Bx (ch 1).
 *   Deck B notes 9x (ch 1), note ids ≈ DA + 0x20 until Shift_State; analog CCs
 *   DA + 8, still ch 1.
 *   Deck C notes 9y (ch 2), same note ids as DA; analog CCs DA + 16, still ch 1.
 *   Deck D notes 9y (ch 2), same note ids as DB; analog CCs DA + 24, still ch 1.
 *   Globals (XFADER, VOL_MAIN, CUE_TO_MIX, AUTOMIX, SCRATCH, FILES, FOLDERS,
 *   UP, DOWN, MIC, Load_DA/DB) on ch 1.
 * Every analog CC, including DC/DD mixer strips and all four jog-scratch CCs,
 * is on channel 1. Only DC/DD notes (and Load_DC / Load_DD) move to channel 2.
 *
 * Two physical platters switch A/C and B/D layers; the appendix still lists
 * four wire families, so all four decks are bound. No SET_DECK_MODE init:
 * hardware is unconfirmed.
 *
 * FX knobs are Incremental CCs. MidiAction has no beat_fx / color_fx, so they
 * bind mixer_channel filter (colour-FX stand-in), same as Pioneer live maps.
 *
 * NOT HARDWARE CONFIRMED.
 *
 * Requirements (mini-PRD):
 *   ✔︎ Play, cue, sync, channel faders, crossfader, per-deck jogs and FX
 *     knobs (as mixer filter) bind the appendix Virtual MIDI In ids.
 *     [if] a binding's (channel, kind, id) is absent from that appendix
 *       [then ⛔️] the map invents a wire
 *     [if] nameMatch misses DJ Console 4-Mx [then ⛔️] the device never maps
 *   ✔︎ Load_DA/DB/DC/DD bind browse_load; UP/DOWN are hinted (no encoder).
 *   ✔︎ LED rules only where the Out table prints 00 : OFF – 7F: ON on the
 *     same note as the input (Play, Cue, Sync, CueSel, K1…K6).
 */

import type {
	ControlHint,
	DeviceMap,
	DeviceMapEntry,
	LedRule,
	MidiBinding,
	MidiSource
} from '$lib/rb/midi/midi-types';
import type { DeckId } from '$lib/rb/deck-slots';
import type { HotCueSlot } from '$lib/rb/hot-cue-types';

/** Every analog / jog / FX CC is Bx, including DC/DD strips. [PDF] Appendix 5/12–6/12. */
const ANALOG_CH = 1;

const HOT_CUE_SLOTS: readonly HotCueSlot[] = ['A', 'B', 'C', 'D', 'E', 'F'];

const note = (ch: number, id: number): MidiSource => ({ ch, kind: 'note', id });
const cc = (id: number): MidiSource => ({ ch: ANALOG_CH, kind: 'cc', id });

/** Per-deck appendix wires. noteCh is 9x (1) or 9y (2); CCs always Bx (1). */
interface DeckWires {
	deck: DeckId;
	noteCh: 1 | 2;
	k1: number;
	cue: number;
	play: number;
	cueSel: number;
	stop: number;
	sync: number;
	/** Deck_DA / Deck_DB only; DC/DD have no Deck row. */
	deckSelect: number | null;
	pitSp: number;
	pitSm: number;
	pitSr: number;
	source: number;
	killT: number;
	killM: number;
	killB: number;
	jogTouch: number;
	shiftState: number;
	shift: number;
	pbm: number;
	pbp: number;
	prev: number;
	next: number;
	load: number;
	shiftedK1: number;
	jogSeek: number;
	fx: number;
	vol: number;
	gain: number;
	pitch: number;
	/** LSB CC is PITCH_*_LSBit; offset = LSB id − MSB id. [PDF] Appendix 6/12. */
	pitchLsbOffset: number;
	treble: number;
	medium: number;
	bass: number;
	jogScr: number;
}

/** [PDF] Appendix 1/12–6/12 Virtual MIDI In, default x=0 / y=1. */
const DECKS: readonly DeckWires[] = [
	{
		deck: 1,
		noteCh: 1,
		k1: 0x01,
		shiftedK1: 0x07,
		cue: 0x0d,
		play: 0x0e,
		cueSel: 0x0f,
		stop: 0x10,
		sync: 0x11,
		deckSelect: 0x12,
		pitSp: 0x13,
		pitSm: 0x14,
		pitSr: 0x15,
		source: 0x16,
		killT: 0x17,
		killM: 0x18,
		killB: 0x19,
		jogTouch: 0x1a,
		shiftState: 0x1b,
		shift: 0x1c,
		pbm: 0x1d,
		pbp: 0x1e,
		prev: 0x1f,
		next: 0x20,
		load: 0x4a,
		jogSeek: 0x01,
		fx: 0x02,
		vol: 0x03,
		gain: 0x04,
		pitch: 0x05,
		pitchLsbOffset: 35,
		treble: 0x06,
		medium: 0x07,
		bass: 0x08,
		jogScr: 0x24
	},
	{
		deck: 2,
		noteCh: 1,
		k1: 0x21,
		shiftedK1: 0x27,
		cue: 0x2d,
		play: 0x2e,
		cueSel: 0x2f,
		stop: 0x30,
		sync: 0x31,
		deckSelect: 0x32,
		pitSp: 0x33,
		pitSm: 0x34,
		pitSr: 0x35,
		source: 0x36,
		killT: 0x37,
		killM: 0x38,
		killB: 0x39,
		jogTouch: 0x3a,
		shiftState: 0x3b,
		shift: 0x42,
		pbm: 0x43,
		pbp: 0x44,
		prev: 0x45,
		next: 0x46,
		load: 0x4b,
		jogSeek: 0x09,
		fx: 0x0a,
		vol: 0x0b,
		gain: 0x0c,
		pitch: 0x0d,
		pitchLsbOffset: 28,
		treble: 0x0e,
		medium: 0x0f,
		bass: 0x10,
		jogScr: 0x25
	},
	{
		deck: 3,
		noteCh: 2,
		k1: 0x01,
		shiftedK1: 0x07,
		cue: 0x0d,
		play: 0x0e,
		cueSel: 0x0f,
		stop: 0x10,
		sync: 0x11,
		deckSelect: null,
		pitSp: 0x13,
		pitSm: 0x14,
		pitSr: 0x15,
		source: 0x16,
		killT: 0x17,
		killM: 0x18,
		killB: 0x19,
		jogTouch: 0x1a,
		shiftState: 0x1b,
		shift: 0x1c,
		pbm: 0x1d,
		pbp: 0x1e,
		prev: 0x1f,
		next: 0x20,
		load: 0x4a,
		jogSeek: 0x11,
		fx: 0x12,
		vol: 0x13,
		gain: 0x14,
		pitch: 0x15,
		pitchLsbOffset: 21,
		treble: 0x16,
		medium: 0x17,
		bass: 0x18,
		jogScr: 0x26
	},
	{
		deck: 4,
		noteCh: 2,
		k1: 0x21,
		shiftedK1: 0x27,
		cue: 0x2d,
		play: 0x2e,
		cueSel: 0x2f,
		stop: 0x30,
		sync: 0x31,
		deckSelect: null,
		pitSp: 0x33,
		pitSm: 0x34,
		pitSr: 0x35,
		source: 0x36,
		killT: 0x37,
		killM: 0x38,
		killB: 0x39,
		jogTouch: 0x3a,
		shiftState: 0x3b,
		shift: 0x42,
		pbm: 0x43,
		pbp: 0x44,
		prev: 0x45,
		next: 0x46,
		load: 0x4b,
		jogSeek: 0x19,
		fx: 0x1a,
		vol: 0x1b,
		gain: 0x1c,
		pitch: 0x1d,
		pitchLsbOffset: 14,
		treble: 0x1e,
		medium: 0x1f,
		bass: 0x20,
		jogScr: 0x27
	}
];

function _jog(deck: DeckId, id: number): MidiBinding {
	return {
		source: cc(id),
		action: { type: 'deck_jog', deck },
		relative: true
	};
}

function _deckBindings(w: DeckWires): MidiBinding[] {
	const { deck, noteCh } = w;
	const n = (id: number) => note(noteCh, id);
	return [
		// [PDF] Appendix 1/12–4/12 Play_D* / Cue_D* / Sync_D*.
		{ source: n(w.play), action: { type: 'deck_play_toggle', deck } },
		{ source: n(w.cue), action: { type: 'deck_cue', deck } },
		{ source: n(w.sync), action: { type: 'deck_sync_toggle', deck } },
		// [PDF] CueSel_D* = PFL.
		{ source: n(w.cueSel), action: { type: 'channel_cue', deck } },
		// [PDF] Shift_D*. DB/DD skip 0x3C–0x41 (globals), so Shift_DB is 0x42.
		{ source: n(w.shift), action: { type: 'shift_modifier' } },
		// [PDF] Appendix 3/12 Load_DA/DB; 5/12 Load_DC/DD.
		{ source: n(w.load), action: { type: 'browse_load', deck } },
		// [PDF] K1…K6_D*. Software assigns pads to slots A–F.
		...HOT_CUE_SLOTS.map((slot, i): MidiBinding => ({
			source: n(w.k1 + i),
			action: { type: 'deck_hot_cue', deck, slot }
		})),
		// [PDF] Appendix 5/12–6/12 JOG_SEEK Incremental; JOG_SCR scratch-when-pressed.
		_jog(deck, w.jogSeek),
		_jog(deck, w.jogScr),
		// [PDF] FX_* Incremental. Colour-FX stand-in: mixer filter.
		{
			source: cc(w.fx),
			action: { type: 'mixer_channel', deck, target: 'filter' },
			relative: true
		},
		{ source: cc(w.vol), action: { type: 'mixer_channel', deck, target: 'fader' } },
		{ source: cc(w.gain), action: { type: 'mixer_channel', deck, target: 'trim' } },
		// [PDF] PITCH_* MSB + PITCH_*_LSBit (0x28–0x2B). LSBit is 0/1, not 7-bit.
		{ source: cc(w.pitch), action: { type: 'deck_pitch', deck, lsbOffset: w.pitchLsbOffset } },
		{
			source: cc(w.treble),
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'high' }
		},
		{
			source: cc(w.medium),
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'mid' }
		},
		{
			source: cc(w.bass),
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'low' }
		},
		// [PDF] PBM / PBP = pitch bend minus / plus.
		{ source: n(w.pbm), action: { type: 'deck_tempo_nudge', deck, direction: -1 } },
		{ source: n(w.pbp), action: { type: 'deck_tempo_nudge', deck, direction: 1 } }
	];
}

const _GLOBAL_BINDINGS: MidiBinding[] = [
	// [PDF] Appendix 6/12 XFADER / VOL_MAIN / CUE_TO_MIX, all Bx.
	{ source: cc(0x22), action: { type: 'mixer_global', target: 'crossfader' } },
	{ source: cc(0x21), action: { type: 'mixer_global', target: 'master' } },
	{ source: cc(0x23), action: { type: 'headphone_mix' } }
];

function _deckHints(w: DeckWires): ControlHint[] {
	const { deck, noteCh } = w;
	const d = `(deck ${deck})`;
	const n = (id: number, label: string): ControlHint => ({
		source: note(noteCh, id),
		label: `${label} ${d}`
	});
	const hints: ControlHint[] = [
		n(w.stop, 'Stop'),
		n(w.source, 'SOURCE'),
		n(w.killT, 'KILL_T'),
		n(w.killM, 'KILL_M'),
		n(w.killB, 'KILL_B'),
		n(w.jogTouch, 'JOG_TOUCH'),
		n(w.shiftState, 'Shift_State'),
		n(w.pitSp, 'PIT_SP'),
		n(w.pitSm, 'PIT_SM'),
		n(w.pitSr, 'PIT_SR'),
		n(w.prev, 'Prev'),
		n(w.next, 'Next')
	];
	if (w.deckSelect !== null) hints.push(n(w.deckSelect, 'Deck'));
	for (let i = 0; i < 6; i++) hints.push(n(w.shiftedK1 + i, `SHIFTED_K${i + 1}`));
	return hints;
}

/** [PDF] Appendix 3/12 globals on 9x. UP/DOWN are notes; there is no browse encoder. */
const _GLOBAL_HINTS: ControlHint[] = [
	{ source: note(1, 0x3c), label: 'AUTOMIX' },
	{ source: note(1, 0x3d), label: 'SCRATCH' },
	{ source: note(1, 0x3e), label: 'FILES' },
	{ source: note(1, 0x3f), label: 'FOLDERS' },
	{ source: note(1, 0x40), label: 'UP' },
	{ source: note(1, 0x41), label: 'DOWN' },
	{ source: note(1, 0x47), label: 'MIC' },
	{ source: note(1, 0x48), label: 'DECK_STATE_AC' },
	{ source: note(1, 0x49), label: 'DECK_STATE_BD' }
];

function _led(ch: number, noteId: number, trigger: LedRule['trigger']): LedRule {
	return {
		trigger,
		out: { ch, note: noteId, velocityOn: 0x7f, velocityOff: 0x00 }
	};
}

function _deckLeds(w: DeckWires): LedRule[] {
	const { deck, noteCh } = w;
	return [
		// [PDF] Appendix 7/12–9/12: same note as input, 00 OFF / 7F ON.
		_led(noteCh, w.play, { kind: 'deck_playing', deck }),
		_led(noteCh, w.cue, { kind: 'deck_loaded', deck }),
		_led(noteCh, w.sync, { kind: 'beat_sync_enabled', deck }),
		_led(noteCh, w.cueSel, { kind: 'channel_cue_enabled', deck }),
		...HOT_CUE_SLOTS.map((slot, i) =>
			_led(noteCh, w.k1 + i, { kind: 'hot_cue_present', deck, slot })
		)
	];
}

export const HERCULES_DJ_CONSOLE_4MX_MAP: DeviceMap = {
	vendor: 'Hercules',
	nameMatch: 'DJ Console 4-Mx',
	bindings: [...DECKS.flatMap(_deckBindings), ..._GLOBAL_BINDINGS],
	leds: DECKS.flatMap(_deckLeds),
	hints: [...DECKS.flatMap(_deckHints), ..._GLOBAL_HINTS]
};

/** Self-registration: maps/entries.ts discovers this export, so no shared list names this map. */
export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{ key: 'HERCULES_DJ_CONSOLE_4MX_MAP', map: HERCULES_DJ_CONSOLE_4MX_MAP, live: true }
];
