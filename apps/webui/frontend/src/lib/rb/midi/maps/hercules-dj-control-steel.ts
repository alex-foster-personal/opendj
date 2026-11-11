/**
 * Hercules DJ Control Steel live app MIDI map (issue #10842).
 *
 * SOURCE OF TRUTH for every (channel, kind, id) in this file: the Hercules
 * DJ Control Steel English user manual, section 9 "MIDI STANDARD SUPPORT",
 * pages 20-22 (messages sent) and 23-25 (messages received / LEDs):
 * https://ts.hercules.com/download/sound/manuals/DJSteel/01_DJ%20Control%20Steel_Eng.pdf
 * (not redistributed here).
 *
 * Also cited: section 6.6 Effects and loops (buttons 1-6), 6.15 Control 1
 * and 2 encoders, 6.16 Apply Ctrl. On button (FX surface); 6.8 Cue Select,
 * 6.9 Browser buttons, 6.11 Monitor knob (cue / browse / cue-mix); 6.5 /
 * 6.2 Master Tempo (Beatlock). The Traktor 3 setup note
 * https://ts.hercules.com/faqs/eng/her_eng_00369.pdf is used ONLY for the
 * USB product string `Hercules DJ Control Steel` and factory MIDI channel 1.
 * Control numbers are never taken from that note.
 *
 * Mixxx's mapping is GPL; this repo is Apache 2.0. Do not copy it.
 * tools/deck-diagrams/devices/hercules-dj-control-steel/midi.json is Mixxx
 * ingest and is not a wire source.
 *
 * Channel: every message is Control Change `Bx NN Value`. `x` is the MIDI
 * channel nibble set in the Hercules control panel; factory / Traktor
 * default is channel 1. Every source here is `{ ch: 1, kind: 'cc', id }`.
 *
 * Buttons are "Button - Toggling Output, 7F Pressed / 00 Released", so every
 * button binding sets `ccButton: true`. Jog and the Control / Select
 * encoders are "Incremental – Coarse" two's-complement ticks (0x01 = +1 CW
 * slow, 0x7F = -1 CCW slow; same pattern as hercules-dj-console-4-mx) →
 * `relative` only, not `relativeCentered`. Pitch / volume /
 * EQ / gain / crossfader / master / cue-mix / headphone volume are
 * "Analog – Coarse (128 values)". Pitch is 7-bit (`lsbOffset: null`). Chart
 * pitch and volume are `00 Full Down > 7F Full Up`; bind without `invert`.
 *
 * TWO DECKS, NOT FOUR: nothing here addresses deck 3/4.
 *
 * FX RESOLUTION: MidiAction has no beat_fx or color_fx type. Pioneer colour
 * FX in this repo is `mixer_channel` `filter`, which takeoverContinuous
 * consumes as an absolute 0..1 (`continuous01` throws on `relative`).
 * Control 1/2 (PDF 6.15, CC 0x41-0x44) are hinted incremental CCs (unbound),
 * so they cannot bind to `filter`. Keypad 1-6 (PDF 6.6, software-assignable)
 * are Open DJ's P0 pad surface: hot cues A-F, with shift 7-8 as G-H.
 * Control 1/2 and Apply Ctrl (PDF 6.16, CC 0x70-0x72) are hints. A later
 * contract change is the follow-up that would bind them as beat/colour FX.
 *
 * LEDs: MIDI-OUT (pages 23-25) lights buttons with CC (`Bx NN`, 00 off /
 * 7F on). LedRule.out and sendLed emit Note On, which will not light this
 * hardware. No LED rules in this map. Do not put CC numbers into
 * LedRule.out.note.
 *
 * NOT HARDWARE CONFIRMED.
 */

import type {
	ControlHint,
	DeviceMap,
	DeviceMapEntry,
	MidiAction,
	MidiBinding,
	MidiSource
} from '$lib/rb/midi/midi-types';
import type { HotCueSlot } from '$lib/rb/hot-cue-types';

const DECKS = [1, 2] as const;
type SteelDeck = (typeof DECKS)[number];
const MIDI_CH = 1;
const HOT_CUE_SLOTS_AF: readonly HotCueSlot[] = ['A', 'B', 'C', 'D', 'E', 'F'];

function cc(id: number): MidiSource {
	return { ch: MIDI_CH, kind: 'cc', id };
}

function ccButton(id: number, action: MidiAction): MidiBinding {
	return { source: cc(id), action, ccButton: true };
}

/** Left (deck 1) and right (deck 2) send-table ids from section 9. */
interface DeckWires {
	keypad: readonly [number, number, number, number, number, number];
	sync: number;
	beatlock: number;
	play: number;
	cue: number;
	load: number;
	headphoneCue: number;
	hotCueG: number;
	hotCueH: number;
	jog: number;
	pitch: number;
	volume: number;
	gain: number;
	treble: number;
	medium: number;
	bass: number;
	previous: number;
	next: number;
	stop: number;
	killTreble: number;
	killMedium: number;
	killBass: number;
	reset: number;
	source: number;
	shiftKey3to6: readonly [number, number, number, number];
	fxControl1: number;
	fxControl2: number;
}

const DECK_WIRES: Record<SteelDeck, DeckWires> = {
	1: {
		keypad: [0x01, 0x02, 0x03, 0x04, 0x05, 0x06],
		sync: 0x07,
		beatlock: 0x08,
		play: 0x0b,
		cue: 0x0c,
		load: 0x12,
		headphoneCue: 0x14,
		hotCueG: 0x64,
		hotCueH: 0x65,
		jog: 0x2f,
		pitch: 0x31,
		volume: 0x32,
		gain: 0x33,
		treble: 0x34,
		medium: 0x35,
		bass: 0x36,
		previous: 0x09,
		next: 0x0a,
		stop: 0x0d,
		killTreble: 0x0e,
		killMedium: 0x0f,
		killBass: 0x10,
		reset: 0x11,
		source: 0x13,
		shiftKey3to6: [0x66, 0x67, 0x68, 0x69],
		fxControl1: 0x41,
		fxControl2: 0x42
	},
	2: {
		keypad: [0x19, 0x1a, 0x1b, 0x1c, 0x1d, 0x1e],
		sync: 0x1f,
		beatlock: 0x15,
		play: 0x23,
		cue: 0x24,
		load: 0x16,
		headphoneCue: 0x18,
		hotCueG: 0x6a,
		hotCueH: 0x6b,
		jog: 0x30,
		pitch: 0x3b,
		volume: 0x3c,
		gain: 0x3d,
		treble: 0x3e,
		medium: 0x3f,
		bass: 0x40,
		previous: 0x21,
		next: 0x22,
		stop: 0x25,
		killTreble: 0x26,
		killMedium: 0x27,
		killBass: 0x28,
		reset: 0x20,
		source: 0x17,
		shiftKey3to6: [0x6c, 0x6d, 0x6e, 0x6f],
		fxControl1: 0x43,
		fxControl2: 0x44
	}
};

function _deckBindings(deck: SteelDeck): MidiBinding[] {
	const w = DECK_WIRES[deck];
	return [
		// [PDF] §9 keypad 1..6; §6.6 software-assignable → P0 hot cues A-F.
		...HOT_CUE_SLOTS_AF.map((slot, i) =>
			ccButton(w.keypad[i], { type: 'deck_hot_cue', deck, slot })
		),
		// [PDF] §9 Sync.
		ccButton(w.sync, { type: 'deck_sync_toggle', deck }),
		// [PDF] §9 Beatlock; §6.5 / §6.2 Master Tempo.
		ccButton(w.beatlock, { type: 'deck_key_sync_toggle', deck }),
		// [PDF] §9 Play/Pause.
		ccButton(w.play, { type: 'deck_play_toggle', deck }),
		// [PDF] §9 Cue.
		ccButton(w.cue, { type: 'deck_cue', deck }),
		// [PDF] §9 Load; §6.9 Browser.
		ccButton(w.load, { type: 'browse_load', deck }),
		// [PDF] §9 Headphone Cue; §6.8 Cue Select.
		ccButton(w.headphoneCue, { type: 'channel_cue', deck }),
		// [PDF] §9 Shift + Key 1..2 (= 7..8) → hot cues G/H.
		ccButton(w.hotCueG, { type: 'deck_hot_cue', deck, slot: 'G' }),
		ccButton(w.hotCueH, { type: 'deck_hot_cue', deck, slot: 'H' }),
		// [PDF] §9 Jog Wheel, Incremental – Coarse (two's-complement relative).
		{
			source: cc(w.jog),
			action: { type: 'deck_jog', deck },
			relative: true
		},
		// [PDF] §9 Pitch, Analog – Coarse, 7-bit.
		{ source: cc(w.pitch), action: { type: 'deck_pitch', deck, lsbOffset: null } },
		// [PDF] §9 Volume / Gain / Treble / Medium / Bass.
		{ source: cc(w.volume), action: { type: 'mixer_channel', deck, target: 'fader' } },
		{ source: cc(w.gain), action: { type: 'mixer_channel', deck, target: 'trim' } },
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
		}
	];
}

/** Shared mixer, browse, and both shift statuses. All on channel 1. */
const _GLOBAL_BINDINGS: MidiBinding[] = [
	// [PDF] §9 Main Volume.
	{ source: cc(0x38), action: { type: 'mixer_global', target: 'master' } },
	// [PDF] §9 Cross Fader.
	{ source: cc(0x39), action: { type: 'mixer_global', target: 'crossfader' } },
	// [PDF] §9 Cue Mix; §6.11 Monitor knob.
	{ source: cc(0x3a), action: { type: 'headphone_mix' } },
	// [PDF] §9 VOL_HP.
	{ source: cc(0x47), action: { type: 'headphone_level' } },
	// [PDF] §9 Controls 1 Select A / Controls 2 Select B; §6.9 Browser.
	{
		source: cc(0x45),
		action: { type: 'browse_encoder' },
		relative: true
	},
	{
		source: cc(0x46),
		action: { type: 'browse_encoder' },
		relative: true
	},
	// [PDF] §9 Left / Right Shift Status.
	ccButton(0x73, { type: 'shift_modifier' }),
	ccButton(0x74, { type: 'shift_modifier' })
];

function _deckHints(deck: SteelDeck): ControlHint[] {
	const w = DECK_WIRES[deck];
	return [
		// [PDF] §9 Previous / Next (no seek action).
		{ source: cc(w.previous), label: `Previous (deck ${deck})` },
		{ source: cc(w.next), label: `Next (deck ${deck})` },
		// [PDF] §9 Stop.
		{ source: cc(w.stop), label: `Stop (deck ${deck})` },
		// [PDF] §9 Kill Treble / Medium / Bass (no kill action).
		{ source: cc(w.killTreble), label: `Kill Treble (deck ${deck})` },
		{ source: cc(w.killMedium), label: `Kill Medium (deck ${deck})` },
		{ source: cc(w.killBass), label: `Kill Bass (deck ${deck})` },
		// [PDF] §9 Reset (no pitch-reset action). MIDI-OUT names this pitch bend;
		// the send table names it Reset. Do not bind as deck_tempo_nudge.
		{ source: cc(w.reset), label: `Reset (deck ${deck})` },
		// [PDF] §9 Source.
		{ source: cc(w.source), label: `Source (deck ${deck})` },
		// [PDF] §9 Shift + Key 3..6.
		{ source: cc(w.shiftKey3to6[0]), label: `Keypad 3 + SHIFT (deck ${deck})` },
		{ source: cc(w.shiftKey3to6[1]), label: `Keypad 4 + SHIFT (deck ${deck})` },
		{ source: cc(w.shiftKey3to6[2]), label: `Keypad 5 + SHIFT (deck ${deck})` },
		{ source: cc(w.shiftKey3to6[3]), label: `Keypad 6 + SHIFT (deck ${deck})` },
		// [PDF] §9 Controls 1/2 Deck A/B; §6.15. Relative FX modulators cannot
		// drive mixer_channel filter.
		{ source: cc(w.fxControl1), label: `FX Control 1 (deck ${deck})` },
		{ source: cc(w.fxControl2), label: `FX Control 2 (deck ${deck})` }
	];
}

function _globalHints(): ControlHint[] {
	return [
		// [PDF] §9 Scratch (no vinyl-mode action).
		{ source: cc(0x29), label: 'Scratch' },
		// [PDF] §9 Menu; browse is the Select encoders.
		{ source: cc(0x2a), label: 'Menu Up' },
		{ source: cc(0x2b), label: 'Menu Down' },
		{ source: cc(0x2c), label: 'Menu Left' },
		{ source: cc(0x2d), label: 'Menu Right' },
		// [PDF] §9 Talkover State.
		{ source: cc(0x2e), label: 'Talkover' },
		// [PDF] §9 Balance (no balance action).
		{ source: cc(0x37), label: 'Balance' },
		// [PDF] §9 Apply ctrl on Deck A / B / Select; §6.16.
		{ source: cc(0x70), label: 'Apply FX (deck 1)' },
		{ source: cc(0x71), label: 'Apply FX (deck 2)' },
		{ source: cc(0x72), label: 'Apply FX (select)' }
	];
}

export const HERCULES_DJ_CONTROL_STEEL_MAP: DeviceMap = {
	vendor: 'Hercules',
	nameMatch: 'DJ Control Steel',
	bindings: [...DECKS.flatMap((deck) => _deckBindings(deck)), ..._GLOBAL_BINDINGS],
	leds: [],
	hints: [...DECKS.flatMap((deck) => _deckHints(deck)), ..._globalHints()]
};

/** Self-registration: maps/entries.ts discovers this export, so no shared list names this map. */
export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{
		key: 'HERCULES_DJ_CONTROL_STEEL_MAP',
		map: HERCULES_DJ_CONTROL_STEEL_MAP,
		live: true
	}
];
