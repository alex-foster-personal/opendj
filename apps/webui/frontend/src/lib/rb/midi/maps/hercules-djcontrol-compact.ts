/**
 * Hercules DJControl Compact P0 device map (build unit: Compact map).
 *
 * SOURCE OF TRUTH for EVERY Note/CC number in this file: the official
 * Hercules "DJControl Compact MIDI Mapping Version 1.1" PDF, Midi Input
 * and Midi Output tables, from
 * https://ts.hercules.com/download/sound/manuals/DJC_Compact/DJC_Compact_MIDI_Mapping.pdf
 * (not redistributed here; sha256
 * 3c42f8172aac1568a95df90cfba998180131dd0e888113a2592ad28ce1c887c2).
 * Support hub: https://support.hercules.com/en/product/djcontrolcompact-en/
 * Rows are cited as [PDF] with the sheet's own control names (PLAY_DA, JOG_DA, …).
 *
 * DO NOT source numbers from tools/deck-diagrams/devices/hercules-djcontrol-compact/midi.json
 * (Mixxx ingest) or from Mixxx XML/JS.
 *
 * Channel layout: the PDF writes every row as 9x / Bx. Deck A and Deck B are
 * different note/CC ids on ONE MIDI channel, not two channels. Bind every
 * source with ch: 1 (status 0x90 / 0xB0). PLAY_DA is note 0x21, PLAY_DB is
 * note 0x51 (+0x30). JOG_DA is CC 0x30, JOG_DB is CC 0x31.
 *
 * TWO DECKS ONLY: nothing here addresses deck 3/4.
 *
 * JOG encoding ([PDF] JOG_DA / JOG_DB): 7F>40 CCW slow→fast, 01>3F CW
 * slow→fast. That is two's-complement relative (0x01 = +1, 0x7F = -1), NOT
 * Pioneer centre-64. Bind relative: true and do not set relativeCentered.
 *
 * SHIFT is hardware-encoded: SH_PLAY is a different note than PLAY. Bind
 * shift_modifier on [PDF] SH only so midiState.shiftHeld stays truthful.
 * Do not set shift: true on other bindings.
 *
 * FX pads are this device's FX controls. MidiAction has no beat_fx / color_fx
 * (P0 contract, CTRL-10463). Bind FX_KP1–4 as controller_pad (in-union pad
 * FX; ControllerPadMode includes instant_fx). Do not widen midi-types.ts.
 * Default software pad mode is hot_cue, so an FX pad press also jumps/saves
 * slots A–D until instant_fx is implemented. Do not bind MODE as
 * controller_pad_mode (hardware already cycles CUE/FX/SAM/LOOP). Hint SH_FX
 * so shifted FX notes are not controller_pad shifted: true (that would clear
 * hot cues).
 *
 * There is no HI EQ, trim, filter knob, pitch fader, headphone mix, master,
 * or load button on this chart. Do not invent CC 0x3A / 0x3E.
 *
 * NOT HARDWARE CONFIRMED.
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

const CH = 1;
const DECKS: readonly DeckId[] = [1, 2];

const HOT_CUE_SLOTS: readonly HotCueSlot[] = ['A', 'B', 'C', 'D'];
const FX_PADS = [1, 2, 3, 4] as const;

/** Open DJ software assignment of the four LOOP pads. The PDF does not print lengths. */
export const COMPACT_BEAT_LOOP_PAD_BEATS: readonly number[] = [1, 2, 4, 8];

const LED_ON = 0x7f;
const LED_OFF = 0x00;

const note = (id: number): MidiSource => ({ ch: CH, kind: 'note', id });
const cc = (id: number): MidiSource => ({ ch: CH, kind: 'cc', id });

/** [PDF] Deck B pad/transport notes are Deck A + 0x30 (PLAY_DA 0x21 / PLAY_DB 0x51). */
function _noteOffset(deck: DeckId): number {
	return deck === 2 ? 0x30 : 0;
}

function _deckNoteBindings(deck: DeckId): MidiBinding[] {
	const o = _noteOffset(deck);
	const out: MidiBinding[] = [];
	HOT_CUE_SLOTS.forEach((slot, i) => {
		// [PDF] CUE_KP1_DA…CUE_KP4_DA notes 0x01–0x04; Deck B +0x30.
		out.push({
			source: note(0x01 + o + i),
			action: { type: 'deck_hot_cue', deck, slot }
		});
	});
	FX_PADS.forEach((pad, i) => {
		// [PDF] FX_KP1_DA…FX_KP4_DA notes 0x09–0x0C; Deck B +0x30.
		out.push({
			source: note(0x09 + o + i),
			action: { type: 'controller_pad', deck, pad, shifted: false }
		});
	});
	COMPACT_BEAT_LOOP_PAD_BEATS.forEach((beats, i) => {
		// [PDF] LOOP_KP1_DA…LOOP_KP4_DA notes 0x19–0x1C; Deck B +0x30.
		out.push({
			source: note(0x19 + o + i),
			action: { type: 'deck_beat_loop', deck, beats }
		});
	});
	return [
		...out,
		// [PDF] SH_LOOP_KP1_DA note 0x1D: loop exit. Deck B +0x30.
		{ source: note(0x1d + o), action: { type: 'deck_loop_exit', deck } },
		// [PDF] SH_LOOP_KP2_DA note 0x1E.
		{ source: note(0x1e + o), action: { type: 'deck_loop_scale', deck, factor: 0.5 } },
		// [PDF] SH_LOOP_KP3_DA note 0x1F.
		{ source: note(0x1f + o), action: { type: 'deck_loop_scale', deck, factor: 2 } },
		// [PDF] SH_LOOP_KP4_DA note 0x20.
		{ source: note(0x20 + o), action: { type: 'deck_auto_loop_toggle', deck } },
		// [PDF] PLAY_DA note 0x21 / PLAY_DB note 0x51.
		{ source: note(0x21 + o), action: { type: 'deck_play_toggle', deck } },
		// [PDF] CUE_DA note 0x22 / CUE_DB note 0x52.
		{ source: note(0x22 + o), action: { type: 'deck_cue', deck } },
		// [PDF] SYNC_DA note 0x23 / SYNC_DB note 0x53.
		{ source: note(0x23 + o), action: { type: 'deck_sync_toggle', deck } }
	];
}

function _deckMixerAndJog(deck: DeckId): MidiBinding[] {
	const jogCc = deck === 1 ? 0x30 : 0x31;
	const browseCc = deck === 1 ? 0x37 : 0x38;
	const volCc = deck === 1 ? 0x39 : 0x3d;
	const midCc = deck === 1 ? 0x3b : 0x3f;
	const bassCc = deck === 1 ? 0x3c : 0x40;
	return [
		// [PDF] JOG_DA CC 0x30 / JOG_DB CC 0x31. Two's-complement relative, not centre-64.
		{ source: cc(jogCc), action: { type: 'deck_jog', deck }, relative: true },
		// [PDF] SH + JOG_DA CC 0x37 / SH + JOG_DB CC 0x38. Open DJ browse assignment
		// of the shifted jog (the PDF has no browse encoder).
		{ source: cc(browseCc), action: { type: 'browse_encoder' }, relative: true },
		// [PDF] VOL_DA CC 0x39 / VOL_DB CC 0x3D.
		{ source: cc(volCc), action: { type: 'mixer_channel', deck, target: 'fader' } },
		// [PDF] MEDIUM_DA CC 0x3B / MEDIUM_DB CC 0x3F. No HI EQ on this chart.
		{ source: cc(midCc), action: { type: 'mixer_channel', deck, target: 'eq', band: 'mid' } },
		// [PDF] BASS_DA CC 0x3C / BASS_DB CC 0x40.
		{ source: cc(bassCc), action: { type: 'mixer_channel', deck, target: 'eq', band: 'low' } }
	];
}

const _GLOBAL_BINDINGS: MidiBinding[] = [
	// [PDF] XFADER CC 0x36.
	{ source: cc(0x36), action: { type: 'mixer_global', target: 'crossfader' } },
	// [PDF] SH note 0x2F. Once; shared. Hardware-encoded shift twins send other notes.
	{ source: note(0x2f), action: { type: 'shift_modifier' } }
];

function _deckHints(deck: DeckId): ControlHint[] {
	const o = _noteOffset(deck);
	const hints: ControlHint[] = [];
	for (let n = 1; n <= 4; n++) {
		hints.push({
			source: note(0x05 + o + (n - 1)),
			label: `SHIFT+CUE pad ${n} (deck ${deck})`
		});
		hints.push({
			source: note(0x0d + o + (n - 1)),
			label: `SHIFT+FX pad ${n} (deck ${deck})`
		});
		hints.push({
			source: note(0x11 + o + (n - 1)),
			label: `SAMPLER pad ${n} (deck ${deck}; sampler out of P0)`
		});
		hints.push({
			source: note(0x15 + o + (n - 1)),
			label: `SHIFT+SAMPLER pad ${n} (deck ${deck})`
		});
	}
	hints.push({ source: note(0x24 + o), label: `SHIFT+PLAY (deck ${deck})` });
	hints.push({ source: note(0x25 + o), label: `SHIFT+CUE (deck ${deck})` });
	hints.push({ source: note(0x26 + o), label: `SHIFT+SYNC (deck ${deck})` });
	return hints;
}

const _GLOBAL_HINTS: ControlHint[] = [
	// [PDF] RECORD / SH_RECORD notes 0x2B / 0x2C.
	{ source: note(0x2b), label: 'RECORD' },
	{ source: note(0x2c), label: 'SHIFT+RECORD' },
	// [PDF] AUTO / SH_AUTO notes 0x2D / 0x2E. No load button on this unit.
	{ source: note(0x2d), label: 'AUTO' },
	{ source: note(0x2e), label: 'SHIFT+AUTO' },
	// [PDF] MODE note 0x30. Hardware already cycles CUE/FX/SAM/LOOP.
	{ source: note(0x30), label: 'MODE (hardware CUE/FX/SAM/LOOP cycle)' }
];

function _ledRules(deck: DeckId): LedRule[] {
	const o = _noteOffset(deck);
	const out = (n: number) => ({ ch: CH, note: n, velocityOn: LED_ON, velocityOff: LED_OFF });
	const rules: LedRule[] = [
		// [PDF] Midi Output PLAY_DA / PLAY_DB: same note as MIDI-IN, 00 off / 7F on.
		{ trigger: { kind: 'deck_playing', deck }, out: out(0x21 + o) },
		// [PDF] CUE_DA / CUE_DB lamp. Software choice (ours): lit when a track is loaded.
		{ trigger: { kind: 'deck_loaded', deck }, out: out(0x22 + o) },
		// [PDF] SYNC_DA / SYNC_DB.
		{ trigger: { kind: 'beat_sync_enabled', deck }, out: out(0x23 + o) }
	];
	HOT_CUE_SLOTS.forEach((slot, i) => {
		rules.push({
			trigger: { kind: 'hot_cue_present', deck, slot },
			out: out(0x01 + o + i)
		});
	});
	COMPACT_BEAT_LOOP_PAD_BEATS.forEach((beats, i) => {
		rules.push({
			trigger: { kind: 'loop_beats_engaged', deck, beats },
			out: out(0x19 + o + i)
		});
	});
	return rules;
}

export const HERCULES_DJCONTROL_COMPACT_MAP: DeviceMap = {
	vendor: 'Hercules',
	// Catalog model "Hercules DJControl Compact"; USB names "DJControl Compact" /
	// "Hercules DJControl Compact MIDI". Must miss Inpulse, MIX, Instinct, Starlight.
	nameMatch: 'DJControl Compact',
	bindings: [
		...DECKS.flatMap((deck) => _deckNoteBindings(deck)),
		...DECKS.flatMap((deck) => _deckMixerAndJog(deck)),
		..._GLOBAL_BINDINGS
	],
	leds: DECKS.flatMap((deck) => _ledRules(deck)),
	hints: [...DECKS.flatMap((deck) => _deckHints(deck)), ..._GLOBAL_HINTS],
	// [PDF] Midi Output "Update All Controls" Bx 7F, 7F ON: analogs report on connect.
	init: [[0xb0, 0x7f, 0x7f]]
};

/** Self-registration: maps/entries.ts discovers this export, so no shared list names this map. */
export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{ key: 'HERCULES_DJCONTROL_COMPACT_MAP', map: HERCULES_DJCONTROL_COMPACT_MAP, live: true }
];
