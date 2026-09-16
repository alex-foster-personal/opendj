/**
 * Pioneer DJ DDJ-FLX4 P0 device map (build unit: DDJ-FLX4 map).
 *
 * SOURCE OF TRUTH for EVERY Note/CC number in this file: the official
 * AlphaTheta "DDJ-FLX4 List of MIDI messages" PDF (version 1.0, E1),
 * from https://downloads.support.alphatheta.com/software_info/dj-controllers/DDJ-FLX4/DDJ-FLX4_MIDI_message_List_E1.pdf
 * (not redistributed here; sha256 recorded in
 * docs/controller/research/DDJ-FLX4-MIDI-Message-List.md).
 *
 * DO NOT source numbers from tools/deck-diagrams/devices/ddj-flx4/midi.json -
 * that file is a condensed plate-label stub, not transcribed wire data.
 *
 * Channel layout ([PDF] MIDI channel assignment):
 *   deck 1 / deck 2 (non-pad)    -> channels 1 / 2
 *   EFFECT                       -> channels 5 / 6
 *   BROWSER + global mixer       -> channel 7
 *   performance pads, unshifted  -> channels 8 (deck 1) / 10 (deck 2)
 *   performance pads, shifted    -> channels 9 / 11
 *
 * SHIFT NOTE: the FLX4 encodes shift in HARDWARE - shifted controls send
 * different data1 values and pads move to their own channels. This map
 * binds shift_modifier only so midiState.shiftHeld stays truthful.
 *
 * 14-BIT MIXER CAVEAT: mixer controls bind MSB CC only; LSB CCs land in
 * the learn log as unmapped. True 14-bit pairing is deck_pitch only.
 *
 * TWO DECKS, NOT FOUR: nothing here addresses deck 3/4.
 *
 * FLX4 vs DDJ-400: no 4 BEAT long-press row ([PDF] 1-7 is 4 BEAT/EXIT only
 * at note 0x4D). CFX (filter) is per-deck CC 23/24 on channels 1/2
 * ([PDF] 3-5), not channel 7.
 *
 * OUT OF CONTRACT (hints only): jog, BEAT SYNC (#1777), LOOP IN/OUT,
 * Beat FX, SMART CFX/FADER, MIC LEVEL, Android MONO/STEREO, pad-mode
 * buttons, shifted LOAD/CH CUE/MASTER CUE, mixer LSB CCs, fader-start
 * notes, pad modes other than HOT CUE and BEAT LOOP.
 */

import type { ControlHint, DeviceMap, LedRule, MidiBinding } from '$lib/rb/midi/midi-types';
import type { DeckId } from '$lib/rb/deck-slots';
import type { HotCueSlot } from '$lib/rb/hot-cue-types';
import {
	pioneerCc,
	pioneerDeckChannel,
	pioneerNote,
	pioneerPadChannel
} from './pioneer-deck-bindings';

const DECKS = [1, 2] as const;
const HOT_CUE_SLOTS: readonly HotCueSlot[] = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];

export const FLX4_BEAT_LOOP_PAD_BEATS: readonly number[] = [0.25, 0.5, 1, 2, 4, 8, 16, 32];
export const FLX4_PAD_ON = 0x7f;
export const FLX4_PAD_OFF = 0x00;

// This map only ever binds DECKS (1 and 2, the FLX4's two physical channels).
const CFX_CC: Record<(typeof DECKS)[number], number> = { 1: 0x17, 2: 0x18 };

function _deckBindings(deck: DeckId): MidiBinding[] {
	const ch = pioneerDeckChannel(deck);
	return [
		// [PDF] 1-1 PLAY/PAUSE: note 11 (0x0B).
		{ source: pioneerNote(ch, 0x0b), action: { type: 'deck_play_toggle', deck } },
		// [PDF] 1-2 CUE: note 12 (0x0C).
		{ source: pioneerNote(ch, 0x0c), action: { type: 'deck_cue', deck } },
		// [PDF] 1-3 SHIFT: note 63 (0x3F).
		{ source: pioneerNote(ch, 0x3f), action: { type: 'shift_modifier' } },
		// [PDF] 1-11 TEMPO: CC 0 MSB / CC 32 LSB.
		{ source: pioneerCc(ch, 0x00), action: { type: 'deck_pitch', deck, lsbOffset: 32 } },
		// [PDF] 1-7 4 BEAT / EXIT: note 77 (0x4D). No separate long-press row.
		{ source: pioneerNote(ch, 0x4d), action: { type: 'deck_loop_exit', deck } }
	];
}

function _mixerBindings(deck: (typeof DECKS)[number]): MidiBinding[] {
	const ch = pioneerDeckChannel(deck);
	return [
		// [PDF] 3-3 TRIM: CC 4 MSB.
		{ source: pioneerCc(ch, 0x04), action: { type: 'mixer_channel', deck, target: 'trim' } },
		// [PDF] 3-4 EQ HI: CC 7 MSB.
		{ source: pioneerCc(ch, 0x07), action: { type: 'mixer_channel', deck, target: 'eq', band: 'high' } },
		// [PDF] 3-4 EQ MID: CC 11 MSB.
		{ source: pioneerCc(ch, 0x0b), action: { type: 'mixer_channel', deck, target: 'eq', band: 'mid' } },
		// [PDF] 3-4 EQ LOW: CC 15 MSB.
		{ source: pioneerCc(ch, 0x0f), action: { type: 'mixer_channel', deck, target: 'eq', band: 'low' } },
		// [PDF] 3-7 CH FADER: CC 19 MSB.
		{ source: pioneerCc(ch, 0x13), action: { type: 'mixer_channel', deck, target: 'fader' } },
		// [PDF] 3-5 CFX: CC 23 deck 1 / CC 24 deck 2 on deck channels.
		{ source: pioneerCc(ch, CFX_CC[deck]), action: { type: 'mixer_channel', deck, target: 'filter' } },
		// [PDF] 3-6 CH CUE: note 84 (0x54).
		{ source: pioneerNote(ch, 0x54), action: { type: 'channel_cue', deck } }
	];
}

function _padBindings(deck: DeckId): MidiBinding[] {
	const ch = pioneerPadChannel(deck);
	const bindings: MidiBinding[] = [];
	for (let pad = 0; pad < 8; pad++) {
		// [PDF] 5-5 HOT CUE mode: pad k -> note k-1 (0..7).
		bindings.push({
			source: pioneerNote(ch, pad),
			action: { type: 'deck_hot_cue', deck, slot: HOT_CUE_SLOTS[pad] }
		});
		// [PDF] 5-5 BEAT LOOP mode: notes 96-103 (0x60..0x67).
		bindings.push({
			source: pioneerNote(ch, 0x60 + pad),
			action: { type: 'deck_beat_loop', deck, beats: FLX4_BEAT_LOOP_PAD_BEATS[pad] }
		});
	}
	return bindings;
}

function _browserAndGlobalBindings(): MidiBinding[] {
	const ch = 7;
	return [
		// [PDF] 4-1 BROWSE rotate: CC 64 (0x40).
		{ source: pioneerCc(ch, 0x40), action: { type: 'browse_encoder' }, relative: true },
		// [PDF] 4-1 BROWSE rotate +SHIFT: CC 100 (0x64).
		{ source: pioneerCc(ch, 0x64), action: { type: 'browse_encoder' }, relative: true },
		// [PDF] 4-2 LOAD deck 1: note 70 (0x46).
		{ source: pioneerNote(ch, 0x46), action: { type: 'browse_load', deck: 1 } },
		// [PDF] 4-3 LOAD deck 2: note 71 (0x47).
		{ source: pioneerNote(ch, 0x47), action: { type: 'browse_load', deck: 2 } },
		// [PDF] 3-8 CROSSFADER: CC 31 MSB.
		{ source: pioneerCc(ch, 0x1f), action: { type: 'mixer_global', target: 'crossfader' } },
		// [PDF] 3-1 MASTER LEVEL: CC 8 MSB.
		{ source: pioneerCc(ch, 0x08), action: { type: 'mixer_global', target: 'master' } },
		// [PDF] 3-11 HEADPHONE MIX: CC 12 MSB.
		{ source: pioneerCc(ch, 0x0c), action: { type: 'headphone_mix' } },
		// [PDF] 3-12 HEADPHONE LEVEL: CC 13 MSB.
		{ source: pioneerCc(ch, 0x0d), action: { type: 'headphone_level' } },
		// [PDF] 3-2 MASTER CUE: note 99 (0x63).
		{ source: pioneerNote(ch, 0x63), action: { type: 'master_cue', mode: 'latch' } }
	];
}

function _deckHints(deck: DeckId): ControlHint[] {
	const ch = pioneerDeckChannel(deck);
	return [
		// [PDF] 1-4 JOG DIAL.
		{ source: pioneerCc(ch, 0x22), label: `JOG platter, vinyl on (deck ${deck})` },
		{ source: pioneerCc(ch, 0x23), label: `JOG platter, vinyl off (deck ${deck})` },
		{ source: pioneerCc(ch, 0x29), label: `JOG platter + SHIFT (deck ${deck})` },
		{ source: pioneerCc(ch, 0x21), label: `JOG wheel side (deck ${deck})` },
		{ source: pioneerNote(ch, 0x36), label: `JOG touch (deck ${deck})` },
		{ source: pioneerNote(ch, 0x67), label: `JOG touch + SHIFT (deck ${deck})` },
		// [PDF] 1-10 BEAT SYNC (out of scope #1777).
		{ source: pioneerNote(ch, 0x58), label: `BEAT SYNC (deck ${deck})` },
		{ source: pioneerNote(ch, 0x5c), label: `BEAT SYNC long press (deck ${deck})` },
		{ source: pioneerNote(ch, 0x60), label: `BEAT SYNC + SHIFT (deck ${deck})` },
		// [PDF] 1-5 IN / 1-6 OUT.
		{ source: pioneerNote(ch, 0x10), label: `IN (deck ${deck})` },
		{ source: pioneerNote(ch, 0x4c), label: `IN + SHIFT (deck ${deck})` },
		{ source: pioneerNote(ch, 0x11), label: `OUT (deck ${deck})` },
		{ source: pioneerNote(ch, 0x4e), label: `OUT + SHIFT (deck ${deck})` },
		// [PDF] 1-7 4 BEAT/EXIT +SHIFT.
		{ source: pioneerNote(ch, 0x50), label: `4 BEAT/EXIT + SHIFT (deck ${deck})` },
		// [PDF] 1-8 / 1-9 CUE/LOOP CALL.
		{ source: pioneerNote(ch, 0x51), label: `CUE/LOOP CALL back (deck ${deck})` },
		{ source: pioneerNote(ch, 0x3e), label: `CUE/LOOP CALL back + SHIFT (deck ${deck})` },
		{ source: pioneerNote(ch, 0x53), label: `CUE/LOOP CALL forward (deck ${deck})` },
		{ source: pioneerNote(ch, 0x3d), label: `CUE/LOOP CALL forward + SHIFT (deck ${deck})` },
		// [PDF] 3-6 CH CUE +SHIFT.
		{ source: pioneerNote(ch, 0x68), label: `CH CUE + SHIFT (deck ${deck})` }
	];
}

function _globalHints(): ControlHint[] {
	const fx = 5;
	const fx2 = 6;
	const ch = 7;
	return [
		// [PDF] 4-1 BROWSE press.
		{ source: pioneerNote(ch, 0x41), label: 'BROWSE press' },
		{ source: pioneerNote(ch, 0x42), label: 'BROWSE press + SHIFT' },
		// [PDF] 4-2 / 4-3 LOAD +SHIFT.
		{ source: pioneerNote(ch, 0x68), label: 'LOAD deck 1 + SHIFT' },
		{ source: pioneerNote(ch, 0x7a), label: 'LOAD deck 2 + SHIFT' },
		// [PDF] 3-2 MASTER CUE +SHIFT.
		{ source: pioneerNote(ch, 0x78), label: 'MASTER CUE + SHIFT' },
		// [PDF] 3-9 MIC LEVEL.
		{ source: pioneerCc(ch, 0x05), label: 'MIC LEVEL' },
		// [PDF] 3-10 SMART CFX.
		{ source: pioneerNote(ch, 0x00), label: 'SMART CFX' },
		{ source: pioneerNote(ch, 0x08), label: 'SMART CFX + SHIFT' },
		// [PDF] 3-13 SMART FADER.
		{ source: pioneerNote(ch, 0x01), label: 'SMART FADER' },
		{ source: pioneerNote(ch, 0x09), label: 'SMART FADER + SHIFT' },
		// [PDF] 3-14 Android MONO/STEREO.
		{ source: pioneerNote(ch, 0x6d), label: 'Android MONO/STEREO' },
		// [PDF] 2-1..2-6 Beat FX.
		{ source: pioneerNote(fx, 0x10), label: 'FX CH SELECT' },
		{ source: pioneerNote(fx, 0x4a), label: 'FX SELECT' },
		{ source: pioneerNote(fx, 0x64), label: 'FX SELECT + SHIFT' },
		{ source: pioneerNote(fx, 0x4b), label: 'BEAT FX beat back' },
		{ source: pioneerNote(fx, 0x66), label: 'BEAT FX beat back + SHIFT' },
		{ source: pioneerNote(fx, 0x4c), label: 'BEAT FX beat forward' },
		{ source: pioneerNote(fx, 0x6b), label: 'BEAT FX beat forward + SHIFT' },
		{ source: pioneerCc(fx2, 0x02), label: 'FX LEVEL/DEPTH' },
		{ source: pioneerNote(fx, 0x47), label: 'FX ON/OFF' },
		{ source: pioneerNote(fx, 0x43), label: 'FX ON/OFF + SHIFT' }
	];
}

function _modeButtonHints(deck: DeckId): ControlHint[] {
	const ch = pioneerDeckChannel(deck);
	return [
		// [PDF] 5-1..5-4 pad mode buttons.
		{ source: pioneerNote(ch, 0x1b), label: `HOT CUE MODE (deck ${deck})` },
		{ source: pioneerNote(ch, 0x69), label: `HOT CUE MODE + SHIFT (deck ${deck})` },
		{ source: pioneerNote(ch, 0x1e), label: `PAD FX 1 MODE (deck ${deck})` },
		{ source: pioneerNote(ch, 0x6b), label: `PAD FX 1 MODE + SHIFT (deck ${deck})` },
		{ source: pioneerNote(ch, 0x20), label: `BEAT JUMP MODE (deck ${deck})` },
		{ source: pioneerNote(ch, 0x6d), label: `BEAT JUMP MODE + SHIFT (deck ${deck})` },
		{ source: pioneerNote(ch, 0x22), label: `SAMPLER MODE (deck ${deck})` },
		{ source: pioneerNote(ch, 0x6f), label: `SAMPLER MODE + SHIFT (deck ${deck})` }
	];
}

function _ledRules(deck: DeckId): LedRule[] {
	const ch = pioneerDeckChannel(deck);
	const padCh = pioneerPadChannel(deck);
	const rules: LedRule[] = [
		// [PDF] 1-1 PLAY MIDI-OUT.
		{
			trigger: { kind: 'deck_playing', deck },
			out: { ch, note: 0x0b, velocityOn: 0x7f, velocityOff: 0x00 }
		},
		// [PDF] 1-2 CUE MIDI-OUT.
		{
			trigger: { kind: 'deck_loaded', deck },
			out: { ch, note: 0x0c, velocityOn: 0x7f, velocityOff: 0x00 }
		},
		// [PDF] 1-7 4 BEAT/EXIT MIDI-OUT.
		{
			trigger: { kind: 'loop_engaged', deck },
			out: { ch, note: 0x4d, velocityOn: 0x7f, velocityOff: 0x00 }
		},
		// [PDF] 3-6 CH CUE MIDI-OUT.
		{
			trigger: { kind: 'channel_cue_enabled', deck },
			out: { ch, note: 0x54, velocityOn: 0x7f, velocityOff: 0x00 }
		}
	];
	for (let pad = 0; pad < 8; pad++) {
		rules.push({
			trigger: { kind: 'hot_cue_present', deck, slot: HOT_CUE_SLOTS[pad] },
			out: { ch: padCh, note: pad, velocityOn: FLX4_PAD_ON, velocityOff: FLX4_PAD_OFF }
		});
	}
	return rules;
}

export const FLX4_MAP: DeviceMap = {
	vendor: 'Pioneer DJ',
	nameMatch: 'DDJ-FLX4',
	bindings: [
		...DECKS.flatMap((deck) => _deckBindings(deck)),
		...DECKS.flatMap((deck) => _mixerBindings(deck)),
		...DECKS.flatMap((deck) => _padBindings(deck)),
		..._browserAndGlobalBindings()
	],
	leds: DECKS.flatMap((deck) => _ledRules(deck)),
	hints: [
		...DECKS.flatMap((deck) => _deckHints(deck)),
		...DECKS.flatMap((deck) => _modeButtonHints(deck)),
		..._globalHints()
	]
};
