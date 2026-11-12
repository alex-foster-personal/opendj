/**
 * Hercules DJ Console Mk2 live device map (issue #10833).
 *
 * SOURCE OF TRUTH: Hercules "DJ Console MK2 : Operation Manual", section 7
 * "MIDI STANDARD SUPPORT", pages 18-20 of 24 (input rows p.18-19, output p.20),
 * cited as [MAN]. Original manufacturer URL
 * ftp://ftp.hercules.com/sound/manuals/Mk2/01_DJ%20Console%20Mk2_eng.pdf (no longer
 * served). Readable mirror of the same pages:
 * https://www.manualshelf.com/manual/hercules/dj-console-mk2/user-manual-english/page-18.html
 * (pages 19-20 same path). The manual is not redistributed.
 *
 * Encoding: every control is status B0 (CC, MIDI channel 1). Buttons send 7F
 * pressed / 00 released (ccButton). Jogs and pitch knobs send 01..3F CW and
 * 7F..40 CCW (two's complement relative, not centred). Faders, EQ and crossfader
 * are absolute 00..7F.
 *
 * Out of contract: no Sync label (AutoBeat Select is bound to deck_sync_toggle).
 * Pitch knobs are relative so hinted only. Headphone Split/Mix are toggles so
 * hinted. FX buttons are hinted because MidiAction has no beat-FX or colour-FX
 * case and the Mk2 FX controls are momentary buttons. LED outputs on [MAN] p.20
 * are CC so LedRule (note-only out) cannot drive them. No encoder or load button,
 * so no browse binding.
 *
 * NOT HARDWARE CONFIRMED.
 *
 * Requirements (mini-PRD):
 *   ✔︎ Play, cue, AutoBeat (sync), pitch bend, PFL, jogs, faders, EQ and crossfader
 *     bind the [MAN] section 7 CCs on channel 1.
 *     [if] a binding's (channel, kind, id) is absent from [MAN] section 7
 *       [then ⛔️] the map invents a wire
 *     [if] nameMatch misses "Hercules DJ Console MK2" or hits another DJ Console
 *       [then ⛔️] wrong device
 */

import type {
	ControlHint,
	DeviceMap,
	DeviceMapEntry,
	MidiBinding,
	MidiSource
} from '$lib/rb/midi/midi-types';
import type { DeckId } from '$lib/rb/deck-slots';

const ANALOG_CH = 1;

const cc = (id: number): MidiSource => ({ ch: ANALOG_CH, kind: 'cc', id });

interface DeckWires {
	deck: DeckId;
	play: number;
	cue: number;
	autoBeat: number;
	bendMinus: number;
	bendPlus: number;
	headphone: number;
	jog: number;
	volume: number;
	bass: number;
	medium: number;
	treble: number;
	prev: number;
	next: number;
	mouse: number;
	masterTempo: number;
	fxSelect: number;
	fx1: number;
	fx2: number;
	fx3: number;
	source: number;
	pitchKnob: number;
}

const DECKS: readonly DeckWires[] = [
	{
		deck: 1,
		play: 0x08, // [MAN] p.18
		cue: 0x09,
		autoBeat: 0x0a,
		bendMinus: 0x14,
		bendPlus: 0x13,
		headphone: 0x21,
		jog: 0x36, // [MAN] p.19
		volume: 0x32,
		bass: 0x2e,
		medium: 0x2f,
		treble: 0x30,
		prev: 0x0b,
		next: 0x0c,
		mouse: 0x1b,
		masterTempo: 0x16,
		fxSelect: 0x07,
		fx1: 0x0f,
		fx2: 0x0e,
		fx3: 0x0d,
		source: 0x1d,
		pitchKnob: 0x34
	},
	{
		deck: 2,
		play: 0x02, // [MAN] p.18
		cue: 0x03,
		autoBeat: 0x04,
		bendMinus: 0x18,
		bendPlus: 0x17,
		headphone: 0x22,
		jog: 0x37, // [MAN] p.19
		volume: 0x33,
		bass: 0x2b,
		medium: 0x2c,
		treble: 0x2d,
		prev: 0x05,
		next: 0x06,
		mouse: 0x1c,
		masterTempo: 0x1a,
		fxSelect: 0x01,
		fx1: 0x10,
		fx2: 0x11,
		fx3: 0x12,
		source: 0x1e,
		pitchKnob: 0x35
	}
];

const btn = (id: number, binding: Omit<MidiBinding, 'source'>): MidiBinding => ({
	source: cc(id),
	...binding,
	ccButton: true
});

function _deckBindings(w: DeckWires): MidiBinding[] {
	const { deck } = w;
	return [
		btn(w.play, { action: { type: 'deck_play_toggle', deck } }),
		btn(w.cue, { action: { type: 'deck_cue', deck } }),
		btn(w.autoBeat, { action: { type: 'deck_sync_toggle', deck } }),
		btn(w.bendMinus, { action: { type: 'deck_tempo_nudge', deck, direction: -1 } }),
		btn(w.bendPlus, { action: { type: 'deck_tempo_nudge', deck, direction: 1 } }),
		btn(w.headphone, { action: { type: 'channel_cue', deck } }),
		{ source: cc(w.jog), action: { type: 'deck_jog', deck }, relative: true },
		{ source: cc(w.volume), action: { type: 'mixer_channel', deck, target: 'fader' } },
		{ source: cc(w.bass), action: { type: 'mixer_channel', deck, target: 'eq', band: 'low' } },
		{ source: cc(w.medium), action: { type: 'mixer_channel', deck, target: 'eq', band: 'mid' } },
		{ source: cc(w.treble), action: { type: 'mixer_channel', deck, target: 'eq', band: 'high' } }
	];
}

function _deckHints(w: DeckWires): ControlHint[] {
	const { deck } = w;
	return [
		{ source: cc(w.prev), label: `Prev Track (deck ${deck})` },
		{ source: cc(w.next), label: `Next Track (deck ${deck})` },
		{ source: cc(w.mouse), label: `Mouse Button (deck ${deck})` },
		{ source: cc(w.masterTempo), label: `Master Tempo (deck ${deck})` },
		{ source: cc(w.fxSelect), label: `FX Select (deck ${deck})` },
		{ source: cc(w.fx1), label: `FX 1 (deck ${deck})` },
		{ source: cc(w.fx2), label: `FX 2 (deck ${deck})` },
		{ source: cc(w.fx3), label: `FX 3 (deck ${deck})` },
		{ source: cc(w.source), label: `Audio Source Select (deck ${deck})` },
		{ source: cc(w.pitchKnob), label: `Pitch Knob (deck ${deck})` }
	];
}

const _GLOBAL_BINDINGS: MidiBinding[] = [
	{ source: cc(0x31), action: { type: 'mixer_global', target: 'crossfader' } } // [MAN] p.19
];

const _GLOBAL_HINTS: ControlHint[] = [
	{ source: cc(0x23), label: 'Headphone Split' },
	{ source: cc(0x24), label: 'Headphone Mix' },
	{ source: cc(0x28), label: 'Ministick X' },
	{ source: cc(0x39), label: 'Ministick Y' }
];

export const HERCULES_DJ_CONSOLE_MK2_MAP: DeviceMap = {
	vendor: 'Hercules',
	nameMatch: 'DJ Console\\s*Mk\\s*2\\b',
	bindings: [...DECKS.flatMap(_deckBindings), ..._GLOBAL_BINDINGS],
	leds: [],
	hints: [...DECKS.flatMap(_deckHints), ..._GLOBAL_HINTS]
};

/** Self-registration: maps/entries.ts discovers this export, so no shared list names this map. */
export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{ key: 'HERCULES_DJ_CONSOLE_MK2_MAP', map: HERCULES_DJ_CONSOLE_MK2_MAP, live: true }
];
