/**
 * Reloop Mixtour Pro map.
 *
 * Wire source: tools/deck-diagrams/devices/reloop-mixtour-pro/midi.json,
 * derived from the manufacturer map and corrected by physical captures at
 * sayak-brm/ReloopMixxxtourPro commit 45ce7cda. This is an independent Open DJ
 * implementation; the unlicensed Mixxx source is comparative behavior
 * research, not copied code.
 *
 * Channel families are 1-based: N transport/mixer = deck 1..4, P pads/other =
 * 5..8, E effects = 9..12, G global = 16.
 */

import type {
	ControlHint,
	ControllerPadMode,
	DeviceMap,
	LedRule,
	MidiBinding,
	MidiMeterOutput
} from '$lib/rb/midi/midi-types';
import type { DeckId } from '$lib/rb/deck-slots';
import type { HotCueSlot } from '$lib/rb/hot-cue-types';
import { CONTROLLER_LOOP_BEATS } from '$lib/rb/midi/controller-loop-pads';

const PAD_MODES: readonly ControllerPadMode[] = [
	'hot_cue',
	'bounce_loop',
	'pitch_cue',
	'instant_fx',
	'auto_loop',
	'sampler',
	'saved_loops',
	'neural_mix'
];

const HOT_CUE_SLOTS: readonly HotCueSlot[] = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];
const HOT_CUE_COLORS = [0x43, 0x4c, 0x70, 0x7f, 0x7c, 0x46, 0x49, 0x52] as const;

function _deckHints(deck: DeckId): ControlHint[] {
	const n = deck;
	const p = 4 + deck;
	const e = 8 + deck;
	return [
		{ source: { ch: n, kind: 'note', id: 0x08 }, label: `MODE + LOAD deck-layer report (deck ${deck}; hardware-routed)` },
		{ source: { ch: p, kind: 'note', id: 0x08 }, label: `MODE hold (deck ${deck}; hardware modifier)` },
		{ source: { ch: p, kind: 'note', id: 0x10 }, label: `SHIFT + LOAD library section (deck ${deck}; not available)` },
		{ source: { ch: p, kind: 'note', id: 0x25 }, label: `SHIFT + Neural Mix view (deck ${deck}; not available)` },
		{ source: { ch: p, kind: 'note', id: 0x26 }, label: `SHIFT + MODE (deck ${deck}; no documented function)` },
		{ source: { ch: n, kind: 'note', id: 0x0b }, label: `PITCH BEND + hold (deck ${deck}; not available)` },
		{ source: { ch: n, kind: 'note', id: 0x0c }, label: `PITCH BEND - hold (deck ${deck}; not available)` },
		{ source: { ch: n, kind: 'note', id: 0x12 }, label: `Crossfader FX cue layer (deck ${deck}; not available)` },
		{ source: { ch: n, kind: 'note', id: 0x2c }, label: `LOOP IN move mode (deck ${deck}; not available)` },
		{ source: { ch: n, kind: 'note', id: 0x2e }, label: `LOOP OUT move mode (deck ${deck}; not available)` },
		{ source: { ch: n, kind: 'note', id: 0x2d }, label: `crossfader-start report (deck ${deck}; not available)` },
		{ source: { ch: n, kind: 'note', id: 0x31 }, label: `line-fader start report (deck ${deck}; not available)` },
		{ source: { ch: n, kind: 'cc', id: 0x06 }, label: `MODE + browse seek/loop-out adjust (deck ${deck}; not available)` },
		{ source: { ch: e, kind: 'note', id: 0x00 }, label: `SHIFT + FX paddle backspin (deck ${deck}; not available)` },
		{ source: { ch: e, kind: 'cc', id: 0x03 }, label: `MODE + FX parameter (deck ${deck}; not available)` },
		{ source: { ch: e, kind: 'note', id: 0x05 }, label: `FX paddle (deck ${deck}; not available)` },
		{ source: { ch: e, kind: 'note', id: 0x0a }, label: `SHIFT + FX PARAM previous (deck ${deck}; undocumented)` },
		{ source: { ch: e, kind: 'note', id: 0x0b }, label: `MODE + FX SELECT previous (deck ${deck}; not available)` },
		{ source: { ch: e, kind: 'note', id: 0x0c }, label: `MODE + FX SELECT next (deck ${deck}; not available)` },
		{ source: { ch: e, kind: 'note', id: 0x0d }, label: `SHIFT + FX PARAM next (deck ${deck}; undocumented)` }
	];
}

const GLOBAL_HINTS: ControlHint[] = [
	{ source: { ch: 16, kind: 'note', id: 0x00 }, label: 'SHIFT hold (hardware modifier)' },
	{ source: { ch: 16, kind: 'cc', id: 0x01 }, label: 'SHIFT + browse section scroll (not available)' },
	{ source: { ch: 16, kind: 'cc', id: 0x02 }, label: 'FX DRY/WET (not available)' },
	{ source: { ch: 16, kind: 'note', id: 0x03 }, label: 'FX PARAM previous (not available)' },
	{ source: { ch: 16, kind: 'note', id: 0x04 }, label: 'FX PARAM next (not available)' },
	{ source: { ch: 16, kind: 'note', id: 0x06 }, label: 'browse press/open (not available)' },
	{ source: { ch: 16, kind: 'note', id: 0x07 }, label: 'SHIFT + browse BACK (not available)' },
	{ source: { ch: 16, kind: 'note', id: 0x09 }, label: 'SPLIT pad-bank mode (hardware-routed)' },
	{ source: { ch: 16, kind: 'note', id: 0x0a }, label: 'SHIFT + SPLIT (undocumented)' },
	{ source: { ch: 16, kind: 'note', id: 0x7f }, label: 'MONO/STEREO report (hardware state only)' }
];

function _deckBindings(deck: DeckId): MidiBinding[] {
	const n = deck;
	const p = 4 + deck;
	return [
		{ source: { ch: n, kind: 'note', id: 0x00 }, action: { type: 'deck_play_toggle', deck } },
		{ source: { ch: n, kind: 'note', id: 0x01 }, action: { type: 'deck_cue', deck } },
		{ source: { ch: n, kind: 'note', id: 0x02 }, action: { type: 'deck_sync_toggle', deck } },
		{ source: { ch: n, kind: 'note', id: 0x03 }, action: { type: 'deck_manual_loop_cycle', deck } },
		{ source: { ch: n, kind: 'note', id: 0x40 }, action: { type: 'deck_beat_loop', deck, beats: 4 } },
		{ source: { ch: n, kind: 'note', id: 0x2a }, action: { type: 'deck_loop_scale', deck, factor: 0.5 } },
		{ source: { ch: n, kind: 'note', id: 0x2b }, action: { type: 'deck_loop_scale', deck, factor: 2 } },
		{ source: { ch: n, kind: 'note', id: 0x29 }, action: { type: 'deck_key_sync_toggle', deck } },
		{ source: { ch: n, kind: 'note', id: 0x27 }, action: { type: 'deck_key_nudge', deck, semitones: -1 } },
		{ source: { ch: n, kind: 'note', id: 0x28 }, action: { type: 'deck_key_nudge', deck, semitones: 1 } },
		{ source: { ch: n, kind: 'note', id: 0x2f }, action: { type: 'deck_tempo_nudge', deck, direction: -1 } },
		{ source: { ch: n, kind: 'note', id: 0x30 }, action: { type: 'deck_tempo_nudge', deck, direction: 1 } },
		{ source: { ch: n, kind: 'note', id: 0x1b }, action: { type: 'channel_cue', deck } },
		{ source: { ch: p, kind: 'note', id: 0x0a }, action: { type: 'browse_load', deck } },
		{ source: { ch: p, kind: 'note', id: 0x24 }, action: { type: 'deck_stem_eq_toggle', deck } },
		{ source: { ch: n, kind: 'cc', id: 0x16 }, action: { type: 'mixer_channel', deck, target: 'trim' } },
		{ source: { ch: n, kind: 'cc', id: 0x17 }, action: { type: 'mixer_channel', deck, target: 'eq', band: 'high' } },
		{ source: { ch: n, kind: 'cc', id: 0x18 }, action: { type: 'mixer_channel', deck, target: 'eq', band: 'mid' } },
		{ source: { ch: n, kind: 'cc', id: 0x19 }, action: { type: 'mixer_channel', deck, target: 'eq', band: 'low' } },
		{ source: { ch: n, kind: 'cc', id: 0x1a }, action: { type: 'mixer_channel', deck, target: 'filter' } },
		{ source: { ch: n, kind: 'cc', id: 0x1c }, action: { type: 'mixer_channel', deck, target: 'fader' } },
		...PAD_MODES.map((mode, index): MidiBinding => ({
			source: { ch: p, kind: 'note', id: index },
			action: { type: 'controller_pad_mode', deck, mode }
		})),
		...HOT_CUE_SLOTS.flatMap((_slot, index): MidiBinding[] => [
			{
				source: { ch: p, kind: 'note', id: 0x14 + index },
				action: { type: 'controller_pad', deck, pad: (index + 1) as 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8, shifted: false }
			},
			{
				source: { ch: p, kind: 'note', id: 0x1c + index },
				action: { type: 'controller_pad', deck, pad: (index + 1) as 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8, shifted: true }
			}
		])
	];
}

const GLOBAL_BINDINGS: MidiBinding[] = [
	{
		source: { ch: 16, kind: 'cc', id: 0x00 },
		action: { type: 'browse_encoder' },
		relative: true,
		relativeUnit: true,
		invert: true
	},
	{ source: { ch: 16, kind: 'cc', id: 0x08 }, action: { type: 'mixer_global', target: 'crossfader' } },
	{ source: { ch: 16, kind: 'cc', id: 0x0a }, action: { type: 'mixer_global', target: 'master' } },
	{ source: { ch: 16, kind: 'cc', id: 0x0c }, action: { type: 'headphone_level' } },
	{ source: { ch: 16, kind: 'cc', id: 0x0d }, action: { type: 'headphone_mix' } }
];

function _deckLeds(deck: DeckId): LedRule[] {
	const n = deck;
	const p = 4 + deck;
	const dim = deck <= 2 ? 0x02 : 0x01;
	const full = deck <= 2 ? 0x7e : 0x7d;
	const rules: LedRule[] = [
		{ trigger: { kind: 'stem_eq_enabled', deck }, out: { ch: p, note: 0x24, velocityOn: 0x7f, velocityOff: 0x01 } },
		{ trigger: { kind: 'deck_playing', deck }, out: { ch: n, note: 0x00, velocityOn: full, velocityOff: dim } },
		// The engine has no separate cue-indicator bit; keep the proved idle
		// colour rather than pretending loaded means cue-active.
		{ trigger: { kind: 'deck_loaded', deck }, out: { ch: n, note: 0x01, velocityOn: dim, velocityOff: dim } },
		{ trigger: { kind: 'beat_sync_enabled', deck }, out: { ch: n, note: 0x02, velocityOn: full, velocityOff: dim } },
		{ trigger: { kind: 'loop_engaged', deck }, out: { ch: n, note: 0x03, velocityOn: full, velocityOff: dim } },
		{ trigger: { kind: 'channel_cue_enabled', deck }, out: { ch: n, note: 0x1b, velocityOn: 0x7f, velocityOff: 0x01 } }
	];
	for (const [index, mode] of PAD_MODES.entries()) {
		rules.push({
			trigger: { kind: 'pad_mode_selected', deck, mode },
			out: { ch: p, note: index, velocityOn: 0x7f, velocityOff: 0x00 }
		});
	}
	for (const [index, slot] of HOT_CUE_SLOTS.entries()) {
		for (const note of [0x14 + index, 0x1c + index]) {
			rules.push({
				trigger: { kind: 'hot_cue_present', deck, slot, padMode: 'hot_cue' },
				out: { ch: p, note, velocityOn: HOT_CUE_COLORS[index], velocityOff: 0x00 }
			});
		}
	}
	for (const padMode of ['auto_loop', 'bounce_loop'] as const) {
		const velocityOff = padMode === 'auto_loop' ? 3 : 11;
		for (const [index, beats] of CONTROLLER_LOOP_BEATS[padMode].entries()) {
			for (const note of [0x14 + index, 0x1c + index]) {
				rules.push({
					trigger: { kind: 'loop_beats_engaged', deck, beats },
					padMode,
					out: { ch: p, note, velocityOn: velocityOff + 64, velocityOff }
				});
			}
		}
	}
	// Firmware reveals these separate banks only while MODE / SHIFT+MODE is
	// held. Initialising them to dim purple makes their meaning visible.
	for (const note of [0x28, 0x27, 0x2b, 0x2a, 0x30, 0x2f, 0x2e, 0x2c]) {
		rules.push({
			trigger: { kind: 'deck_loaded', deck },
			out: { ch: n, note, velocityOn: 0x03, velocityOff: 0x03 }
		});
	}
	return rules;
}

export const MIXTOUR_PRO_METERS: readonly MidiMeterOutput[] = ([1, 2, 3, 4] as const).map(
	(deck) => ({ deck, out: { ch: deck, cc: 0x1f, maxValue: 6 } })
);

export const RELOOP_MIXTOUR_PRO_MAP: DeviceMap = {
	vendor: 'Reloop',
	nameMatch: '\\bMixtour\\s+Pro\\b',
	nativeAudioProfile: 'master12-cue34',
	bindings: [
		..._deckBindings(1),
		..._deckBindings(2),
		..._deckBindings(3),
		..._deckBindings(4),
		...GLOBAL_BINDINGS
	],
	leds: [..._deckLeds(1), ..._deckLeds(2), ..._deckLeds(3), ..._deckLeds(4)],
	meters: [...MIXTOUR_PRO_METERS],
	hints: [
		..._deckHints(1),
		..._deckHints(2),
		..._deckHints(3),
		..._deckHints(4),
		...GLOBAL_HINTS
	]
};
