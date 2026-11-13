/**
 * Pioneer DJ DDJ-SB P0 device map (practice replica, issue #10858).
 *
 * SOURCE OF TRUTH for EVERY Note/CC number in this file: the official
 * "DDJ-SB List of MIDI messages" PDF (ver. 1.00), from
 * https://downloads.support.alphatheta.com/software_info/dj-controllers/DDJ-SB/DDJ-SB_List_of_MIDI_Messages_E.pdf
 * (not redistributed here; sha256 recorded in
 * docs/controller/research/DDJ-SB-MIDI-Message-List.md). Rows are cited
 * as [PDF] with the sheet's own Fig. ids (1..38).
 *
 * DO NOT source numbers from tools/deck-diagrams/devices/ddj-sb/midi.json -
 * that file is a `"bootstrap": true` stub that cites the later DDJ-SB3 list.
 *
 * Channel layout ([PDF] p.1 "MIDI channel No."):
 *   deck 1 / 2 (other than pads)   -> channels 1 / 2
 *   EFFECT FX1 / FX2               -> channels 5 / 6
 *   BROWSER + global mixer         -> channel 7
 *   performance pads               -> channels 8 (deck 1) / 9 (deck 2)
 *
 * The original SB is a 2-channel Serato beginner unit: 4 pads per deck, not 8,
 * and pad channels 8/9, not the later Pioneer 8/10 pattern. Do not call
 * pioneerPadChannel or pioneerDeckBindings (those assume 8 pads, pad ch 8/10,
 * and a dedicated AUTO LOOP button the SB does not have).
 *
 * SHIFT is a per-deck rubber in the pad bank: [PDF] fig 8, note 0x3F on the
 * deck channel. Two shift_modifier bindings (one per deck) is the same pattern
 * as DDJ-400; the sources differ.
 *
 * 14-BIT MIXER CAVEAT: mixer CCs are MSB/LSB pairs. P0 pairs 14-bit only for
 * deck_pitch, so mixer rows bind the MSB and the LSB CCs land in the learn log.
 *
 * ANALOG / LOCAL LED (no MIDI-IN row, or "LED are lit by DDJ-SB"): MASTER
 * LEVEL (fig 16), HEADPHONES VOL (fig 18), FILTER FADE (fig 19). The replica
 * draws them with `midi: null` (wire unavailable, unwired and inert); this map
 * neither binds nor hints them, so no number is invented for them.
 *
 * LED rules exist only for rows whose [PDF] MIDI-OUT column prints
 * "Same as MIDI-IN" AND whose control is bound: PLAY, CUE, SYNC, CH CUE,
 * HOT CUE pads, AUTO LOOP pads, HOT CUE / AUTO LOOP / SAMPLER mode buttons.
 * SHIFT, VINYL and KEYLOCK print local LED only, so no rule drives them.
 *
 * OUT OF CONTRACT (hints only): jog, VINYL, KEYLOCK, FILTER FADE, MASTER
 * LEVEL, HEADPHONES VOL, FX ON and LEVEL/BEATS, BACK, BROWSE press, MANUAL
 * LOOP mode, shifted twins, pad modes other than the bound HOT CUE row,
 * sampler/manual-loop/roll pad notes, LSB companion CCs, fader-start notes.
 */

import type {
	ControlHint,
	DeviceMap,
	DeviceMapEntry,
	LedRule,
	MidiBinding
} from '$lib/rb/midi/midi-types';
import type { HotCueSlot } from '$lib/rb/hot-cue-types';
import { pioneerCc, pioneerDeckChannel, pioneerNote } from './pioneer-deck-bindings';

const DECKS = [1, 2] as const;
type Deck = (typeof DECKS)[number];

const GLOBAL_CH = 7;
const FX_CH: Record<Deck, number> = { 1: 5, 2: 6 };
/** [PDF] fig 14 FILTER (CH1) / (CH2): CC 23 / 24 MSB on channel 7. */
const FILTER_CC: Record<Deck, number> = { 1: 0x17, 2: 0x18 };

/** [PDF] p.1 pad table: unshifted pads on channels 8 (deck 1) / 9 (deck 2). */
function sbPadChannel(deck: Deck): number {
	return 7 + deck;
}

/** Beats per AUTO LOOP pad 1..4 ([PDF] p.2 notes 0x10..0x13). The sheet
 * documents only the wire notes; the ladder is our software assignment. */
export const DDJ_SB_AUTO_LOOP_PAD_BEATS: readonly number[] = [1, 2, 4, 8];
export const DDJ_SB_PAD_ON = 0x7f;
export const DDJ_SB_PAD_OFF = 0x00;

const HOT_CUE_SLOTS: readonly HotCueSlot[] = ['A', 'B', 'C', 'D'];

function _deckBindings(deck: Deck): MidiBinding[] {
	const ch = pioneerDeckChannel(deck);
	return [
		// [PDF] fig 1 PLAY/PAUSE: note 11 (0x0B).
		{ source: pioneerNote(ch, 0x0b), action: { type: 'deck_play_toggle', deck } },
		// [PDF] fig 2 CUE: note 12 (0x0C).
		{ source: pioneerNote(ch, 0x0c), action: { type: 'deck_cue', deck } },
		// [PDF] fig 3 SYNC: note 88 (0x58).
		{ source: pioneerNote(ch, 0x58), action: { type: 'deck_sync_toggle', deck } },
		// [PDF] fig 8 SHIFT: note 63 (0x3F) on this deck channel.
		{ source: pioneerNote(ch, 0x3f), action: { type: 'shift_modifier' } },
		// [PDF] fig 5 TEMPO: CC 0 MSB / CC 32 LSB, "-" side Min, "+" side Max.
		{ source: pioneerCc(ch, 0x00), action: { type: 'deck_pitch', deck, lsbOffset: 32 } },
		// [PDF] fig 35 HOT CUE mode: note 27 (0x1B).
		{
			source: pioneerNote(ch, 0x1b),
			action: { type: 'controller_pad_mode', deck, mode: 'hot_cue' }
		},
		// [PDF] fig 36 AUTO LOOP mode: note 30 (0x1E).
		{
			source: pioneerNote(ch, 0x1e),
			action: { type: 'controller_pad_mode', deck, mode: 'auto_loop' }
		},
		// [PDF] fig 38 SAMPLER mode: note 34 (0x22).
		{
			source: pioneerNote(ch, 0x22),
			action: { type: 'controller_pad_mode', deck, mode: 'sampler' }
		}
	];
}

function _mixerBindings(deck: Deck): MidiBinding[] {
	const ch = pioneerDeckChannel(deck);
	return [
		// [PDF] fig 11 EQ HI: CC 7 MSB.
		{
			source: pioneerCc(ch, 0x07),
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'high' }
		},
		// [PDF] fig 12 EQ MID: CC 11 MSB.
		{
			source: pioneerCc(ch, 0x0b),
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'mid' }
		},
		// [PDF] fig 13 EQ LOW: CC 15 MSB.
		{
			source: pioneerCc(ch, 0x0f),
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'low' }
		},
		// [PDF] fig 10 CH FADER: CC 19 MSB, "bottom end 0, top end 16383".
		{ source: pioneerCc(ch, 0x13), action: { type: 'mixer_channel', deck, target: 'fader' } },
		// [PDF] fig 14 FILTER (CHn): CC 23 / 24 MSB on channel 7.
		{
			source: pioneerCc(GLOBAL_CH, FILTER_CC[deck]),
			action: { type: 'mixer_channel', deck, target: 'filter' }
		},
		// [PDF] fig 15 CUE (Headphone): note 84 (0x54).
		{ source: pioneerNote(ch, 0x54), action: { type: 'channel_cue', deck } }
	];
}

function _padBindings(deck: Deck): MidiBinding[] {
	const ch = sbPadChannel(deck);
	const out: MidiBinding[] = [];
	for (let pad = 0; pad < 4; pad++) {
		out.push({
			source: pioneerNote(ch, pad),
			action: { type: 'deck_hot_cue', deck, slot: HOT_CUE_SLOTS[pad] }
		});
		out.push({
			source: pioneerNote(ch, 0x10 + pad),
			action: { type: 'deck_beat_loop', deck, beats: DDJ_SB_AUTO_LOOP_PAD_BEATS[pad] }
		});
	}
	return out;
}

function _globalBindings(): MidiBinding[] {
	const ch = GLOBAL_CH;
	return [
		// [PDF] fig 27 BROWSE rotate: CC 64 (0x40); +SHIFT CC 100 (0x64). Both relative.
		{ source: pioneerCc(ch, 0x40), action: { type: 'browse_encoder' }, relative: true },
		{ source: pioneerCc(ch, 0x64), action: { type: 'browse_encoder' }, relative: true },
		// [PDF] fig 28 / 29 LOAD, deck 1 / deck 2: notes 70 (0x46) / 71 (0x47).
		{ source: pioneerNote(ch, 0x46), action: { type: 'browse_load', deck: 1 } },
		{ source: pioneerNote(ch, 0x47), action: { type: 'browse_load', deck: 2 } },
		// [PDF] fig 9 CROSSFADER: CC 31 (0x1F) MSB on channel 7, Min left, Max right.
		{ source: pioneerCc(ch, 0x1f), action: { type: 'mixer_global', target: 'crossfader' } },
		// [PDF] fig 17 HEADPHONES MIX: CC 5 MSB on channel 7.
		{ source: pioneerCc(ch, 0x05), action: { type: 'headphone_mix' } }
	];
}

function _deckHints(deck: Deck): ControlHint[] {
	const ch = pioneerDeckChannel(deck);
	return [
		// [PDF] fig 1 / 2 / 3 +SHIFT.
		{ source: pioneerNote(ch, 0x47), label: `PLAY/PAUSE + SHIFT (deck ${deck})` },
		{ source: pioneerNote(ch, 0x48), label: `CUE + SHIFT (deck ${deck})` },
		{ source: pioneerNote(ch, 0x5c), label: `SYNC + SHIFT (deck ${deck})` },
		// [PDF] fig 4 JOG (Platter) and (Wheel Side).
		{ source: pioneerCc(ch, 0x22), label: `JOG platter, vinyl on (deck ${deck})` },
		{ source: pioneerCc(ch, 0x23), label: `JOG platter, vinyl off (deck ${deck})` },
		{ source: pioneerCc(ch, 0x1f), label: `JOG platter + SHIFT (deck ${deck})` },
		{ source: pioneerNote(ch, 0x36), label: `JOG touch, vinyl on (deck ${deck})` },
		{ source: pioneerNote(ch, 0x35), label: `JOG touch, vinyl off (deck ${deck})` },
		{ source: pioneerNote(ch, 0x67), label: `JOG touch + SHIFT (deck ${deck})` },
		{ source: pioneerCc(ch, 0x21), label: `JOG wheel side (deck ${deck})` },
		{ source: pioneerCc(ch, 0x26), label: `JOG wheel side + SHIFT (deck ${deck})` },
		// [PDF] fig 5 Tempo +SHIFT: CC 5 MSB / 37 LSB.
		{ source: pioneerCc(ch, 0x05), label: `TEMPO + SHIFT (deck ${deck})` },
		// [PDF] fig 6 VINYL; +SHIFT note 78 (0x4E).
		{ source: pioneerNote(ch, 0x17), label: `VINYL (deck ${deck})` },
		{ source: pioneerNote(ch, 0x4e), label: `VINYL + SHIFT (deck ${deck})` },
		// [PDF] fig 7 KEYLOCK; +SHIFT note 96 (0x60).
		{ source: pioneerNote(ch, 0x1a), label: `KEYLOCK (deck ${deck})` },
		{ source: pioneerNote(ch, 0x60), label: `KEYLOCK + SHIFT (deck ${deck})` },
		// [PDF] fig 10 fader-start notes.
		{ source: pioneerNote(ch, 0x66), label: `fader start PLAY (deck ${deck})` },
		{ source: pioneerNote(ch, 0x51), label: `fader start SYNC (deck ${deck})` },
		{ source: pioneerNote(ch, 0x52), label: `fader start CUE (deck ${deck})` },
		// [PDF] fig 15 CH CUE +SHIFT: note 104 (0x68).
		{ source: pioneerNote(ch, 0x68), label: `CH CUE + SHIFT (deck ${deck})` },
		// [PDF] fig 37 MANUAL LOOP mode; +SHIFT rows of the four mode buttons.
		{ source: pioneerNote(ch, 0x20), label: `MANUAL LOOP MODE (deck ${deck})` },
		{ source: pioneerNote(ch, 0x69), label: `HOT CUE 5-8 MODE (HOT CUE + SHIFT, deck ${deck})` },
		{ source: pioneerNote(ch, 0x6b), label: `ROLL MODE (AUTO LOOP + SHIFT, deck ${deck})` },
		{ source: pioneerNote(ch, 0x6d), label: `HOT LOOP MODE (MANUAL LOOP + SHIFT, deck ${deck})` },
		{ source: pioneerNote(ch, 0x6f), label: `BANK MODE (SAMPLER + SHIFT, deck ${deck})` }
	];
}

function _padHints(deck: Deck): ControlHint[] {
	const ch = sbPadChannel(deck);
	const out: ControlHint[] = [];
	for (let pad = 0; pad < 4; pad++) {
		out.push({
			source: pioneerNote(ch, 0x08 + pad),
			label: `PAD ${pad + 1} HOT CUE delete (deck ${deck})`
		});
		out.push({
			source: pioneerNote(ch, 0x18 + pad),
			label: `PAD ${pad + 1} MANUAL LOOP (deck ${deck})`
		});
		out.push({
			source: pioneerNote(ch, 0x20 + pad),
			label: `PAD ${pad + 1} SAMPLER (deck ${deck})`
		});
	}
	return out;
}

function _fxHints(unit: Deck): ControlHint[] {
	const ch = FX_CH[unit];
	return [
		// [PDF] fig 20/22/25 (FX1) and 21/24/26 (FX2): ON notes 71..73; +SHIFT 99..101.
		{ source: pioneerNote(ch, 0x47), label: `FX ${unit} slot 1 ON` },
		{ source: pioneerNote(ch, 0x63), label: `FX ${unit} slot 1 ON + SHIFT` },
		{ source: pioneerNote(ch, 0x48), label: `FX ${unit} slot 2 ON` },
		{ source: pioneerNote(ch, 0x64), label: `FX ${unit} slot 2 ON + SHIFT` },
		{ source: pioneerNote(ch, 0x49), label: `FX ${unit} slot 3 ON` },
		{ source: pioneerNote(ch, 0x65), label: `FX ${unit} slot 3 ON + SHIFT` },
		// [PDF] fig 23 / 27 LEVEL/BEATS: CC 2 / +SHIFT CC 0.
		{ source: pioneerCc(ch, 0x02), label: `FX ${unit} LEVEL/BEATS` },
		{ source: pioneerCc(ch, 0x00), label: `FX ${unit} BEATS (LEVEL + SHIFT)` }
	];
}

function _globalHints(): ControlHint[] {
	const ch = GLOBAL_CH;
	return [
		// [PDF] fig 27 BROWSE press: note 65 (0x41); +SHIFT note 66 (0x42).
		{ source: pioneerNote(ch, 0x41), label: 'BROWSE press' },
		{ source: pioneerNote(ch, 0x42), label: 'BROWSE press + SHIFT' },
		// [PDF] fig 28 / 29 LOAD +SHIFT: notes 88 (0x58) / 89 (0x59).
		{ source: pioneerNote(ch, 0x58), label: 'LOAD deck 1 + SHIFT' },
		{ source: pioneerNote(ch, 0x59), label: 'LOAD deck 2 + SHIFT' },
		// [PDF] fig 30 BACK: note 101 (0x65); +SHIFT note 102 (0x66).
		{ source: pioneerNote(ch, 0x65), label: 'BACK' },
		{ source: pioneerNote(ch, 0x66), label: 'BACK + SHIFT' }
	];
}

function _ledRules(deck: Deck): LedRule[] {
	const ch = pioneerDeckChannel(deck);
	const padCh = sbPadChannel(deck);
	const on = DDJ_SB_PAD_ON;
	const off = DDJ_SB_PAD_OFF;
	const rules: LedRule[] = [
		// [PDF] fig 1 PLAY/PAUSE MIDI-OUT "Same as MIDI-IN".
		{
			trigger: { kind: 'deck_playing', deck },
			out: { ch, note: 0x0b, velocityOn: on, velocityOff: off }
		},
		// [PDF] fig 2 CUE MIDI-OUT. Software choice (ours): lit when a track is loaded.
		{
			trigger: { kind: 'deck_loaded', deck },
			out: { ch, note: 0x0c, velocityOn: on, velocityOff: off }
		},
		// [PDF] fig 3 SYNC MIDI-OUT.
		{
			trigger: { kind: 'beat_sync_enabled', deck },
			out: { ch, note: 0x58, velocityOn: on, velocityOff: off }
		},
		// [PDF] fig 15 CUE (Headphone) MIDI-OUT.
		{
			trigger: { kind: 'channel_cue_enabled', deck },
			out: { ch, note: 0x54, velocityOn: on, velocityOff: off }
		},
		// [PDF] fig 35 / 36 / 38 pad-mode MIDI-OUT.
		{
			trigger: { kind: 'pad_mode_selected', deck, mode: 'hot_cue' },
			out: { ch, note: 0x1b, velocityOn: on, velocityOff: off }
		},
		{
			trigger: { kind: 'pad_mode_selected', deck, mode: 'auto_loop' },
			out: { ch, note: 0x1e, velocityOn: on, velocityOff: off }
		},
		{
			trigger: { kind: 'pad_mode_selected', deck, mode: 'sampler' },
			out: { ch, note: 0x22, velocityOn: on, velocityOff: off }
		}
	];
	for (let pad = 0; pad < 4; pad++) {
		rules.push({
			trigger: { kind: 'hot_cue_present', deck, slot: HOT_CUE_SLOTS[pad] },
			out: { ch: padCh, note: pad, velocityOn: on, velocityOff: off }
		});
		rules.push({
			trigger: { kind: 'loop_beats_engaged', deck, beats: DDJ_SB_AUTO_LOOP_PAD_BEATS[pad] },
			out: { ch: padCh, note: 0x10 + pad, velocityOn: on, velocityOff: off }
		});
	}
	return rules;
}

export const DDJ_SB_MAP: DeviceMap = {
	vendor: 'Pioneer DJ',
	// Colour suffixes (DDJ-SB-S) match; DDJ-SB2 / DDJ-SB3 must not (no lookahead: regex_lite).
	nameMatch: 'DDJ-SB(?:$|[^0-9])',
	bindings: [
		...DECKS.flatMap((deck) => _deckBindings(deck)),
		...DECKS.flatMap((deck) => _mixerBindings(deck)),
		...DECKS.flatMap((deck) => _padBindings(deck)),
		..._globalBindings()
	],
	leds: DECKS.flatMap((deck) => _ledRules(deck)),
	hints: [
		...DECKS.flatMap((deck) => _deckHints(deck)),
		...DECKS.flatMap((deck) => _padHints(deck)),
		...DECKS.flatMap((unit) => _fxHints(unit)),
		..._globalHints()
	]
};

/** Self-registration: maps/entries.ts discovers this export, so no shared list names this map. */
export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{ key: 'DDJ_SB_MAP', map: DDJ_SB_MAP, live: true }
];
