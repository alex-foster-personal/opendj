/**
 * Pioneer DJ XDJ-XZ device map (practice replica, epic #9366).
 *
 * SOURCE OF TRUTH for every number: the official "XDJ-XZ MIDI Message List"
 * (E1), not redistributed; URL and sha256 in
 * docs/controller/research/XDJ-XZ-MIDI-Message-List.md.
 *
 * The XZ list is wire-for-wire the XDJ-RX3 list for everything XDJ_RX3_MAP
 * binds or hints (deck n -> channel n, pads on channel 5 + n with hot cues at
 * notes 0-7 and beat loops at 16-23, mixer and effects on channel 5, browser
 * on channel 1, CH CUE and MASTER CUE as CC buttons), so this map extends
 * that one rather than retyping it. Checked row by row against the XZ list:
 * [PDF] 1-23/1-24 PLAY/CUE notes 0/1, 1-2 TEMPO CC 0+32, 1-6 SYNC 31,
 * 1-17/1-18/1-19 LOOP IN/OUT/RELOOP 6/7/8, 1-26 LOAD 81, 1-1 jog CC 34/33,
 * 3-5..3-8 TRIM/EQ, 2-14 COLOR, 3-2 CH FADER 17-20, 3-11 CH CUE 70-73,
 * 3-1 CROSSFADER 11, 3-13 MASTER LEVEL 24, 3-9/3-10 HEADPHONES 27/26,
 * 3-12 MASTER CUE 74, 4-1 ROTARY SELECTOR CC 79.
 *
 * XZ-only rows added here: 4 BEAT (note 67, long press 68) as beat loops,
 * ROTARY SELECTOR + SHIFT (CC 96) as the same relative encoder, CUE lit
 * when a track is loaded, 4 BEAT lit while a 4-beat loop runs, and hints for
 * every other top-panel control the list gives a wire (effects, mixer
 * switches, mic EQ, browser buttons, pad-mode buttons, shifted rows), so the
 * practice replica can place them inert instead of inventing bindings.
 *
 * NOT HARDWARE CONFIRMED. SHIFT has no wire in the list (hardware-encoded).
 * The two physical decks are deck 1 (left) and deck 2 (right); decks 3/4 are
 * the same controls after DECK SELECT and keep the RX3 channel-3/4 wires.
 */

import type {
	ControlHint,
	DeviceMap,
	DeviceMapEntry,
	LedRule,
	MidiBinding
} from '$lib/rb/midi/midi-types';
import type { DeckId } from '$lib/rb/deck-slots';
import type { HotCueSlot } from '$lib/rb/hot-cue-types';
import { pioneerCc, pioneerDeckChannel, pioneerNote } from './pioneer-deck-bindings';
import { XDJ_RX3_MAP } from './pioneer-xdj';

const DECKS = [1, 2, 3, 4] as const;
const MIXER_CH = 5;
const BROWSER_CH = 1;
const HOT_CUE_SLOTS: readonly HotCueSlot[] = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];

function _xzDeckBindings(deck: DeckId): MidiBinding[] {
	const ch = pioneerDeckChannel(deck);
	return [
		// [PDF] 1-16 4 BEAT: press 67, long press 68 (the panel prints 4 / 8 BEAT).
		{ source: pioneerNote(ch, 67), action: { type: 'deck_beat_loop', deck, beats: 4 } },
		{ source: pioneerNote(ch, 68), action: { type: 'deck_beat_loop', deck, beats: 8 } }
	];
}

function _xzGlobalBindings(): MidiBinding[] {
	// [PDF] 4-1 ROTARY SELECTOR + SHIFT: CC 96, same relative ticks as CC 79.
	return [
		{ source: pioneerCc(BROWSER_CH, 96), action: { type: 'browse_encoder' }, relative: true }
	];
}

function _xzDeckLeds(deck: DeckId): LedRule[] {
	const ch = pioneerDeckChannel(deck);
	return [
		// [PDF] 1-24 CUE MIDI-OUT; 1-16 4 BEAT MIDI-OUT.
		{
			trigger: { kind: 'deck_loaded', deck },
			out: { ch, note: 1, velocityOn: 0x7f, velocityOff: 0x00 }
		},
		{
			trigger: { kind: 'loop_beats_engaged', deck, beats: 4 },
			out: { ch, note: 67, velocityOn: 0x7f, velocityOff: 0x00 }
		}
	];
}

function _xzDeckHints(deck: DeckId): ControlHint[] {
	const ch = pioneerDeckChannel(deck);
	const padCh = 5 + deck;
	const d = (name: string): string => `${name} (deck ${deck})`;
	return [
		// [PDF] 1-1 jog shifted rows and wheel side + SHIFT.
		{ source: pioneerCc(ch, 41), label: d('JOG platter + SHIFT') },
		{ source: pioneerNote(ch, 72), label: d('JOG touch + SHIFT') },
		{ source: pioneerCc(ch, 38), label: d('JOG wheel side + SHIFT') },
		// [PDF] 1-3/1-5/1-7/1-9/1-10 tempo and jog buttons.
		{ source: pioneerNote(ch, 19), label: d('TEMPO RESET') },
		{ source: pioneerNote(ch, 16), label: d('TEMPO RANGE') },
		{ source: pioneerNote(ch, 30), label: d('MASTER') },
		{ source: pioneerNote(ch, 71), label: d('SYNC long press') },
		{ source: pioneerNote(ch, 18), label: d('JOG MODE') },
		{ source: pioneerCc(ch, 30), label: d('VINYL SPEED ADJUST') },
		// [PDF] 1-17 LOOP IN long press, 1-20..1-22 cue/loop memory.
		{ source: pioneerNote(ch, 69), label: d('LOOP IN long press') },
		{ source: pioneerNote(ch, 11), label: d('CUE/LOOP CALL next') },
		{ source: pioneerNote(ch, 12), label: d('CUE/LOOP CALL previous') },
		{ source: pioneerNote(ch, 13), label: d('CUE/LOOP DELETE') },
		{ source: pioneerNote(ch, 10), label: d('CUE/LOOP MEMORY') },
		// [PDF] 5-1..5-4 pad mode buttons and their shifted rows.
		{ source: pioneerNote(ch, 34), label: d('HOT CUE mode') },
		{ source: pioneerNote(ch, 35), label: d('BEAT LOOP mode') },
		{ source: pioneerNote(ch, 36), label: d('SLIP LOOP mode') },
		{ source: pioneerNote(ch, 37), label: d('BEAT JUMP mode') },
		{ source: pioneerNote(ch, 38), label: d('HOT CUE mode + SHIFT') },
		{ source: pioneerNote(ch, 39), label: d('BEAT LOOP mode + SHIFT') },
		{ source: pioneerNote(ch, 40), label: d('SLIP LOOP mode + SHIFT') },
		{ source: pioneerNote(ch, 41), label: d('BEAT JUMP mode + SHIFT') },
		// [PDF] 5-5..5-12 pads + SHIFT in HOT CUE mode: notes 8-15.
		...HOT_CUE_SLOTS.map((slot, pad): ControlHint => ({
			source: pioneerNote(padCh, 8 + pad),
			label: d(`PAD ${pad + 1} + SHIFT (hot cue ${slot})`)
		}))
	];
}

/** [PDF] group 2 (EFFECT) and the channel-5 mixer rows the RX3 map does not carry. */
function _xzMixerAndEffectHints(): ControlHint[] {
	const cc = (id: number, label: string): ControlHint => ({
		source: pioneerCc(MIXER_CH, id),
		label
	});
	const note = (id: number, label: string): ControlHint => ({
		source: pioneerNote(MIXER_CH, id),
		label
	});
	const fxSelect: [number, string][] = [
		[42, 'DELAY'],
		[55, 'ECHO'],
		[51, 'PING PONG'],
		[43, 'SPIRAL'],
		[54, 'REVERB'],
		[53, 'TRANS'],
		[59, 'FILTER'],
		[50, 'FLANGER'],
		[57, 'PHASER'],
		[63, 'PITCH'],
		[58, 'SPIRAL ROLL'],
		[46, 'ROLL'],
		[61, 'VINYL BRAKE'],
		[62, 'HELIX']
	];
	const chSelect: [number, string][] = [
		[39, 'CROSSFADER A'],
		[40, 'CROSSFADER B'],
		[28, 'MIC 1'],
		[29, 'MIC 2'],
		[38, 'MIC 1/2'],
		[32, 'AUX'],
		[36, 'CH 3'],
		[34, 'CH 1'],
		[35, 'CH 2'],
		[37, 'CH 4'],
		[41, 'MASTER']
	];
	return [
		cc(116, 'X-PAD slider mode'),
		cc(117, 'X-PAD beat mode'),
		cc(76, 'BEAT <'),
		cc(77, 'BEAT >'),
		cc(69, 'AUTO/TAP'),
		cc(78, 'TAP'),
		note(118, 'FX QUANTIZE'),
		cc(102, 'FX FREQUENCY HI'),
		cc(103, 'FX FREQUENCY MID'),
		cc(104, 'FX FREQUENCY LOW'),
		...fxSelect.map(([id, name]) => cc(id, `EFFECT SELECT ${name}`)),
		...chSelect.map(([id, name]) => cc(id, `CH SELECT ${name}`)),
		cc(13, 'TIME (MSB)'),
		cc(45, 'TIME (LSB)'),
		cc(91, 'LEVEL/DEPTH'),
		cc(114, 'BEAT EFFECT ON/OFF'),
		cc(108, 'PARAMETER'),
		cc(105, 'SOUND COLOR FX SPACE'),
		cc(107, 'SOUND COLOR FX DUB ECHO'),
		cc(106, 'SOUND COLOR FX SWEEP'),
		cc(85, 'SOUND COLOR FX NOISE'),
		cc(86, 'SOUND COLOR FX CRUSH'),
		cc(87, 'SOUND COLOR FX FILTER'),
		// [PDF] 3-3 crossfader assign, 3-4 input select, 3-14..3-17 master EQ and booth.
		cc(65, 'CROSSFADER ASSIGN CH 1'),
		cc(66, 'CROSSFADER ASSIGN CH 2'),
		cc(67, 'CROSSFADER ASSIGN CH 3'),
		cc(68, 'CROSSFADER ASSIGN CH 4'),
		note(61, 'CH 3 INPUT computer'),
		note(59, 'CH 3 INPUT line'),
		note(60, 'CH 3 INPUT phono'),
		note(65, 'CH 4 INPUT computer'),
		note(63, 'CH 4 INPUT line'),
		note(64, 'CH 4 INPUT phono'),
		cc(49, 'MASTER EQ LOW'),
		cc(48, 'MASTER EQ MID'),
		cc(47, 'MASTER EQ HI'),
		cc(25, 'BOOTH MONITOR LEVEL'),
		// [PDF] 3-25..3-32 mic EQ, feedback reducer, aux.
		cc(31, 'MIC 1 EQ LOW'),
		cc(98, 'MIC 1 EQ MID'),
		cc(30, 'MIC 1 EQ HI'),
		cc(97, 'MIC 2 EQ LOW'),
		cc(99, 'MIC 2 EQ MID'),
		cc(96, 'MIC 2 EQ HI'),
		cc(88, 'MIC FEEDBACK REDUCER LIGHT/HEAVY'),
		cc(89, 'MIC FEEDBACK REDUCER ON/OFF'),
		cc(119, 'AUX off'),
		cc(120, 'AUX line'),
		cc(121, 'AUX portable'),
		cc(122, 'AUX TRIM')
	];
}

/** [PDF] group 4 (BROWSER), channel 1. */
function _xzBrowserHints(): ControlHint[] {
	const note = (id: number, label: string): ControlHint => ({
		source: pioneerNote(BROWSER_CH, id),
		label
	});
	return [
		note(51, 'ROTARY SELECTOR press'),
		note(97, 'ROTARY SELECTOR press + SHIFT'),
		note(50, 'BACK'),
		note(95, 'BACK + SHIFT'),
		note(48, 'TAG TRACK/REMOVE'),
		note(98, 'TAG TRACK/REMOVE + SHIFT'),
		note(49, 'TRACK FILTER/EDIT'),
		note(93, 'TRACK FILTER/EDIT + SHIFT'),
		note(14, 'TIME MODE'),
		note(15, 'TIME MODE long press'),
		note(9, 'DECK QUANTIZE')
	];
}

const _wireKey = (s: { ch: number; kind: string; id: number }): string =>
	`${s.ch}/${s.kind}/${s.id}`;

/** RX3 hints are shared with XZ; drop rows XZ already binds and fold duplicate sources. */
function _xzMergedHints(bindings: readonly MidiBinding[]): ControlHint[] {
	const bound = new Set(bindings.map((b) => _wireKey(b.source)));
	const seen = new Set<string>();
	const out: ControlHint[] = [];
	for (const hint of [
		...(XDJ_RX3_MAP.hints ?? []),
		...DECKS.flatMap((deck) => _xzDeckHints(deck)),
		..._xzMixerAndEffectHints(),
		..._xzBrowserHints()
	]) {
		const key = _wireKey(hint.source);
		if (bound.has(key) || seen.has(key)) continue;
		seen.add(key);
		out.push(hint);
	}
	return out;
}

const _XZ_BINDINGS: readonly MidiBinding[] = [
	...XDJ_RX3_MAP.bindings,
	...DECKS.flatMap((deck) => _xzDeckBindings(deck)),
	..._xzGlobalBindings()
];

export const XDJ_XZ_MAP: DeviceMap = {
	vendor: 'Pioneer DJ',
	nameMatch: 'XDJ-XZ',
	bindings: [..._XZ_BINDINGS],
	leds: [...(XDJ_RX3_MAP.leds ?? []), ...DECKS.flatMap((deck) => _xzDeckLeds(deck))],
	hints: _xzMergedHints(_XZ_BINDINGS)
};

/** Self-registration: maps/entries.ts discovers this export, so no shared list names this map. */
export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{ key: 'XDJ_XZ_MAP', map: XDJ_XZ_MAP, live: true }
];
