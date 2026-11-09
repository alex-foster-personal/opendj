/**
 * Pioneer DJ DDJ-REV7 P0 device map (build unit: DDJ-REV7 map).
 *
 * SOURCE OF TRUTH for EVERY Note/CC number in this file: the official
 * AlphaTheta "DDJ-REV7 List of MIDI message" PDF (E1, (c) 2022), from
 * https://downloads.support.alphatheta.com/software_info/dj-controllers/DDJ-REV7/DDJ-REV7_MIDI_message_List_E1.pdf
 * (not redistributed here; sha256 recorded in
 * docs/controller/research/DDJ-REV7-MIDI-Message-List.md). Rows are cited
 * as [PDF] with the sheet's own fig ids (D24, M4, B1, E7, P5...).
 *
 * DO NOT source numbers from tools/deck-diagrams/devices/ddj-rev7/midi.json -
 * that file is a condensed plate-label stub, not transcribed wire data.
 *
 * Channel layout ([PDF] MIDI channel assignment):
 *   deck 1 / deck 2 (non-pad, non-slot) -> channels 1 / 2
 *   SLOT/PAD mode buttons D19-D22      -> channels 3 / 4
 *   FX (CH1) / FX (CH2)                -> channels 5 / 6
 *   BROWSER, global mixer, SHIFT, FX   -> channel 7
 *   performance pads                   -> channels 8 (deck 1) / 10 (deck 2)
 *
 * SHIFT NOTE: ONE hardware SHIFT ([PDF] M10) on channel 7 note 63, shared by
 * both decks, unlike the per-deck SHIFT of the DDJ-400/FLX4. Shift is still
 * encoded in hardware (a shifted control sends a different data1), so this
 * map needs no `shift: true` bindings; SHIFT binds shift_modifier only so
 * midiState.shiftHeld stays truthful.
 *
 * NO CUE, NO CH CUE, NO MASTER CUE: the REV7 is a motorised battle
 * controller. Transport is START/STOP ([PDF] D24, note 11, the same number
 * as PLAY/PAUSE on the DDJ-400/FLX4) plus the platter; headphone cueing is
 * the CH1/CH2 cue fader ([PDF] M16). deck_cue, channel_cue and master_cue
 * are therefore NOT bound; nothing here fabricates them.
 *
 * PADS: HOT CUE MODE ([PDF] P5-P12, notes 0-7 on the pad channel) binds
 * deck_hot_cue A-H. The REV7's upper pad notes are NOT beat loops (0x60-0x67
 * is SAVED FLIP MODE), so the shared pioneerPadBindings helper is not used.
 * ROLL, SAVED LOOP, SAMPLER, PITCH PLAY, SLICER LOOP, SAVED FLIP and
 * SCRATCH BANK rows are out of contract; only HOT CUE MODE + SHIFT (notes
 * 8-15) is hinted so the practice replica's shift layer resolves.
 *
 * 14-BIT MIXER CAVEAT (as the DDJ-400/FLX4 maps): mixer and FX knobs bind
 * their MSB CC only; LSB CCs (MSB + 32) land in the learn log as unmapped.
 * True 14-bit pairing is deck_pitch only.
 *
 * OUT OF CONTRACT (hints only): PITCH BEND -/+ (momentary bend, not a tempo
 * nudge; same ruling as the Mixtour Pro), TEMPO RANGE/RESET, KEY LOCK, SLIP,
 * CENSOR, INSTANT SCRATCH, HOT CUE/SCRATCH BANK bank buttons, SLOT 1-4, STOP
 * TIME, 33/45, INPUT SELECT, SILENT CUE, pad-mode and PARAMETER buttons,
 * BROWSE press, BACK, BOOTH, SAMPLER VOLUME, the cue fader, the Beat FX and
 * software FX section (E1-E13), MIC, fader curve and reverse switches, every
 * +SHIFT row, mixer LSB CCs. No wire here is hardware-confirmed yet.
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
import {
	pioneerCc,
	pioneerDeckChannel,
	pioneerNote,
	pioneerPadChannel
} from './pioneer-deck-bindings';

// -------------------------------------------------------------- constants

/** The REV7 addresses decks 1 and 2 only (2-channel unit). */
const DECKS: readonly DeckId[] = [1, 2];

/** Hot-cue pad order: pad k triggers slot HOT_CUE_SLOTS[k-1]. Wire fact:
 * pad k sends note k-1 ([PDF] P5-P12 HOT CUE MODE rows). Lettering is ours. */
const HOT_CUE_SLOTS: readonly HotCueSlot[] = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];

/** Pad LED "on" colour number. [PDF] pad MIDI-OUT rows: "lit in color
 * specified in color number". Which colour each value shows is unpublished,
 * so P0 uses one fixed value, as the FLX10 map does. */
export const REV7_PAD_COLOR_ON = 0x7f;
export const REV7_PAD_COLOR_OFF = 0x00;

/** [PDF] channel table: BROWSER + global mixer + SHIFT + beat FX -> channel 7. */
const GLOBAL_CH = 7;
/** [PDF] channel table: FX (CH1) -> 5, FX (CH2) -> 6. */
const FX_CH: Record<1 | 2, number> = { 1: 5, 2: 6 };
/** [PDF] M5 FILTER: CC 23 (CH 1) / CC 24 (CH 2) on the global channel. */
const FILTER_CC: Record<1 | 2, number> = { 1: 0x17, 2: 0x18 };
/** [PDF] channel table: DECK n SLOT/PAD -> channel 2 + n. */
function _slotChannel(deck: DeckId): number {
	return 2 + deck;
}

// ---------------------------------------------------------------- _helpers

function _deckBindings(deck: DeckId): MidiBinding[] {
	const ch = pioneerDeckChannel(deck);
	return [
		// [PDF] D24 START/STOP: note 11 (0x0B). Starts or stops the deck.
		{ source: pioneerNote(ch, 0x0b), action: { type: 'deck_play_toggle', deck } },
		// [PDF] D11 TEMPO: CC 0 MSB / CC 32 LSB, "-" side Min.
		{ source: pioneerCc(ch, 0x00), action: { type: 'deck_pitch', deck, lsbOffset: 32 } },
		// [PDF] D3 JOG rotate: CC 34 (0x22), "increases from 65, decreases from 63".
		{
			source: pioneerCc(ch, 0x22),
			action: { type: 'deck_jog', deck },
			relative: true,
			relativeCentered: true
		},
		// [PDF] D4 AUTO LOOP, LOOP options OFF row: note 20 (0x14). The LOOP
		// options ON row (note 35) is hinted.
		{ source: pioneerNote(ch, 0x14), action: { type: 'deck_auto_loop_toggle', deck } },
		// [PDF] D5 LOOP 1/2X: note 18 (0x12). D6 LOOP 2X: note 19 (0x13).
		{ source: pioneerNote(ch, 0x12), action: { type: 'deck_loop_scale', deck, factor: 0.5 } },
		{ source: pioneerNote(ch, 0x13), action: { type: 'deck_loop_scale', deck, factor: 2 } },
		// [PDF] D10 SYNC: note 88 (0x58).
		{ source: pioneerNote(ch, 0x58), action: { type: 'deck_sync_toggle', deck } },
		// [PDF] D14 KEY -: note 31 (0x1F). D15 KEY +: note 28 (0x1C).
		{ source: pioneerNote(ch, 0x1f), action: { type: 'deck_key_nudge', deck, semitones: -1 } },
		{ source: pioneerNote(ch, 0x1c), action: { type: 'deck_key_nudge', deck, semitones: 1 } }
	];
}

function _mixerBindings(deck: 1 | 2): MidiBinding[] {
	const ch = pioneerDeckChannel(deck);
	return [
		// [PDF] M4 TRIM: CC 4 MSB.
		{ source: pioneerCc(ch, 0x04), action: { type: 'mixer_channel', deck, target: 'trim' } },
		// [PDF] M6 ISO HI: CC 7 MSB. M7 ISO MID: CC 11. M8 ISO LOW: CC 15.
		{
			source: pioneerCc(ch, 0x07),
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'high' }
		},
		{
			source: pioneerCc(ch, 0x0b),
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'mid' }
		},
		{
			source: pioneerCc(ch, 0x0f),
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'low' }
		},
		// [PDF] M11 CH FADER: CC 19 MSB, "Min at bottom end, Max at top end".
		{ source: pioneerCc(ch, 0x13), action: { type: 'mixer_channel', deck, target: 'fader' } },
		// [PDF] M5 FILTER: CC 23 / 24 on channel 7, NOT the deck channel.
		{
			source: pioneerCc(GLOBAL_CH, FILTER_CC[deck]),
			action: { type: 'mixer_channel', deck, target: 'filter' }
		}
	];
}

function _padBindings(deck: DeckId): MidiBinding[] {
	// [PDF] P5-P12 HOT CUE MODE: notes 0-7 on channel 8 (deck 1) / 10 (deck 2).
	const ch = pioneerPadChannel(deck);
	return HOT_CUE_SLOTS.map((slot, pad) => ({
		source: pioneerNote(ch, pad),
		action: { type: 'deck_hot_cue' as const, deck, slot }
	}));
}

function _globalBindings(): MidiBinding[] {
	const ch = GLOBAL_CH;
	return [
		// [PDF] B1 BROWSE rotate: CC 64 (0x40), +SHIFT CC 100 (0x64); two's
		// complement deltas (1..30 clockwise, 127..98 counterclockwise).
		{ source: pioneerCc(ch, 0x40), action: { type: 'browse_encoder' }, relative: true },
		{ source: pioneerCc(ch, 0x64), action: { type: 'browse_encoder' }, relative: true },
		// [PDF] B3 LOAD: note 70 (0x46) deck 1, note 71 (0x47) deck 2.
		{ source: pioneerNote(ch, 0x46), action: { type: 'browse_load', deck: 1 } },
		{ source: pioneerNote(ch, 0x47), action: { type: 'browse_load', deck: 2 } },
		// [PDF] M12 CROSSFADER: CC 31 (0x1F) MSB, "Min at left side".
		{ source: pioneerCc(ch, 0x1f), action: { type: 'mixer_global', target: 'crossfader' } },
		// [PDF] M3 MASTER LEVEL: CC 8 MSB.
		{ source: pioneerCc(ch, 0x08), action: { type: 'mixer_global', target: 'master' } },
		// [PDF] M15 HEADPHONES MIX: CC 12 MSB. M14 HEADPHONES LEVEL: CC 13 MSB.
		{ source: pioneerCc(ch, 0x0c), action: { type: 'headphone_mix' } },
		{ source: pioneerCc(ch, 0x0d), action: { type: 'headphone_level' } },
		// [PDF] M10 SHIFT: note 63 (0x3F) on channel 7, one button for both decks.
		{ source: pioneerNote(ch, 0x3f), action: { type: 'shift_modifier' } }
	];
}

// ------------------------------------------------------------------- hints

/** Documented-but-unbound deck rows ([PDF] D1-D26, M1, M9, M17, P1-P4,
 * P13-P14 on the deck channel), each with its +SHIFT twin where printed. */
function _deckHints(deck: DeckId): ControlHint[] {
	const ch = pioneerDeckChannel(deck);
	const d = `(deck ${deck})`;
	const rows: [number, string][] = [
		[0x39, 'PITCH BEND -'],
		[0x59, 'PITCH BEND - + SHIFT'],
		[0x3a, 'PITCH BEND +'],
		[0x5d, 'PITCH BEND + + SHIFT'],
		[0x50, 'AUTO LOOP + SHIFT'],
		[0x10, 'LOOP 1/2X + SHIFT'],
		[0x11, 'LOOP 2X + SHIFT'],
		[0x60, 'TEMPO RANGE'],
		[0x34, 'TEMPO RANGE + SHIFT'],
		[0x45, 'TEMPO RESET'],
		[0x37, 'TEMPO RESET + SHIFT'],
		[0x1a, 'KEY LOCK'],
		[0x19, 'KEY LOCK + SHIFT'],
		[0x5c, 'SYNC + SHIFT'],
		[0x40, 'SLIP'],
		[0x17, 'SLIP + SHIFT'],
		[0x15, 'CENSOR'],
		[0x38, 'CENSOR + SHIFT'],
		[0x65, 'KEY - + SHIFT'],
		[0x64, 'KEY + + SHIFT'],
		[0x43, 'INSTANT SCRATCH'],
		[0x44, 'INSTANT SCRATCH + SHIFT'],
		[0x49, 'HOT CUE bank'],
		[0x4a, 'HOT CUE bank + SHIFT'],
		[0x5a, 'SCRATCH BANK'],
		[0x5b, 'SCRATCH BANK + SHIFT'],
		[0x47, 'START/STOP + SHIFT'],
		[0x61, '33 RPM'],
		[0x4b, '33 RPM + SHIFT'],
		[0x62, '45 RPM'],
		[0x4c, '45 RPM + SHIFT'],
		[0x55, 'INPUT SELECT to USB A'],
		[0x56, 'INPUT SELECT to PHONO/LINE'],
		[0x57, 'INPUT SELECT to USB B'],
		[0x73, 'SILENT CUE'],
		[0x77, 'SILENT CUE + SHIFT'],
		[0x36, 'LINE/PHONO switch'],
		[0x1b, 'HOT CUE MODE'],
		[0x69, 'HOT CUE MODE + SHIFT'],
		[0x1e, 'ROLL MODE'],
		[0x6b, 'ROLL MODE + SHIFT'],
		[0x20, 'SAVED LOOP MODE'],
		[0x6d, 'SAVED LOOP MODE + SHIFT'],
		[0x22, 'SAMPLER MODE'],
		[0x6f, 'SAMPLER MODE + SHIFT'],
		[0x24, 'PARAMETER 1 back (HOT CUE MODE)'],
		[0x01, 'PARAMETER 1 back + SHIFT (HOT CUE MODE)'],
		[0x2c, 'PARAMETER 1 forward (HOT CUE MODE)'],
		[0x09, 'PARAMETER 1 forward + SHIFT (HOT CUE MODE)']
	];
	return [
		...rows.map(([id, label]): ControlHint => ({
			source: pioneerNote(ch, id),
			label: `${label} ${d}`
		})),
		// [PDF] D3 JOG + SHIFT: CC 41 (0x29). D11 TEMPO + SHIFT: CC 5 MSB.
		{ source: pioneerCc(ch, 0x29), label: `JOG + SHIFT ${d}` },
		{ source: pioneerCc(ch, 0x05), label: `TEMPO + SHIFT ${d}` },
		// [PDF] D4 AUTO LOOP, LOOP options ON row: note 35 (0x23).
		{ source: pioneerNote(ch, 0x23), label: `AUTO LOOP, LOOP options ON ${d}` },
		// [PDF] D23 STOP TIME: CC 8 MSB on the deck channel.
		{ source: pioneerCc(ch, 0x08), label: `STOP TIME ${d}` }
	];
}

/** [PDF] D19-D22 SLOT 1-4 / PAD 1-4 on the SLOT/PAD channel, HOT CUE MODE
 * rows (notes 0-3, +SHIFT 4-7). Other modes are out of contract. */
function _slotHints(deck: DeckId): ControlHint[] {
	const ch = _slotChannel(deck);
	const out: ControlHint[] = [];
	for (let slot = 0; slot < 4; slot++) {
		out.push({ source: pioneerNote(ch, slot), label: `SLOT ${slot + 1} (deck ${deck})` });
		out.push({
			source: pioneerNote(ch, 4 + slot),
			label: `SLOT ${slot + 1} + SHIFT (deck ${deck})`
		});
	}
	return out;
}

/** [PDF] P5-P12 HOT CUE MODE + SHIFT: notes 8-15 on the pad channel. */
function _padHints(deck: DeckId): ControlHint[] {
	const ch = pioneerPadChannel(deck);
	return HOT_CUE_SLOTS.map((_slot, pad) => ({
		source: pioneerNote(ch, 8 + pad),
		label: `PAD ${pad + 1} + SHIFT, HOT CUE MODE (deck ${deck})`
	}));
}

/** [PDF] E11 / E12 CH1 / CH2 EFFECT LEVER base rows and the lever's
 * software-FX rows, on the FX channels. */
function _fxChannelHints(deck: 1 | 2): ControlHint[] {
	const ch = FX_CH[deck];
	return [
		{ source: pioneerNote(ch, 0x50), label: `CH${deck} EFFECT LEVER` },
		{ source: pioneerNote(ch, 0x47), label: `CH${deck} EFFECT LEVER, software FX slot 1` },
		{ source: pioneerNote(ch, 0x48), label: `CH${deck} EFFECT LEVER, software FX slot 2` },
		{ source: pioneerNote(ch, 0x49), label: `CH${deck} EFFECT LEVER, software FX slot 3` }
	];
}

/** Global (channel 7) unbound controls: browser, mixer, Beat FX section. */
function _globalHints(): ControlHint[] {
	const ch = GLOBAL_CH;
	const notes: [number, string][] = [
		// [PDF] B1 BROWSE press / +SHIFT; B2 BACK / +SHIFT; B3 LOAD + SHIFT.
		[0x41, 'BROWSE press'],
		[0x42, 'BROWSE press + SHIFT'],
		[0x65, 'BACK'],
		[0x66, 'BACK + SHIFT'],
		[0x5d, 'LOAD deck 1 + SHIFT'],
		[0x6d, 'LOAD deck 2 + SHIFT'],
		// [PDF] E1-E6 FX SELECT, BEAT FX selected rows, and their +SHIFT rows.
		[0x00, 'FX SELECT ECHO'],
		[0x01, 'FX SELECT SPIN'],
		[0x02, 'FX SELECT FLANGER'],
		[0x03, 'FX SELECT REVERB'],
		[0x04, 'FX SELECT BRAKE'],
		[0x05, 'FX SELECT DUCK DOWN'],
		[0x0c, 'FX SELECT ECHO + SHIFT'],
		[0x0d, 'FX SELECT SPIN + SHIFT'],
		[0x0e, 'FX SELECT FLANGER + SHIFT'],
		[0x0f, 'FX SELECT REVERB + SHIFT'],
		[0x10, 'FX SELECT BRAKE + SHIFT'],
		[0x11, 'FX SELECT DUCK DOWN + SHIFT'],
		// [PDF] E7 TAP, E8 BEAT back, E9 BEAT forward, E13 SMOOTH ECHO.
		[0x0a, 'TAP'],
		[0x16, 'TAP + SHIFT'],
		[0x06, 'BEAT FX beat back'],
		[0x12, 'BEAT FX beat back + SHIFT'],
		[0x07, 'BEAT FX beat forward'],
		[0x13, 'BEAT FX beat forward + SHIFT'],
		[0x09, 'SMOOTH ECHO'],
		[0x15, 'SMOOTH ECHO + SHIFT'],
		// [PDF] M19 MIC OFF/ON/TALKOVER, M25/M30 fader reverse, M27 crossfader reverse.
		[0x6a, 'MIC switch to OFF'],
		[0x6b, 'MIC switch to ON'],
		[0x6c, 'MIC switch to TALKOVER'],
		[0x19, 'CH1 FADER REVERSE'],
		[0x20, 'CH2 FADER REVERSE'],
		[0x18, 'CROSSFADER REVERSE']
	];
	const ccs: [number, string][] = [
		// [PDF] M2 BOOTH LEVEL, M13 SAMPLER VOLUME, M16 HEADPHONES CUE FADER, E10 LEVEL/DEPTH.
		[0x09, 'BOOTH LEVEL'],
		[0x03, 'SAMPLER VOLUME'],
		[0x0e, 'HEADPHONES CUE FADER (CH1/CH2)'],
		[0x0a, 'FX LEVEL/DEPTH'],
		// [PDF] M22-M24 MIC EQ LOW / HI, MIC ECHO; M26/M31 fader curve; M29 crossfader curve.
		[0x0f, 'MIC EQ LOW'],
		[0x07, 'MIC EQ HI'],
		[0x06, 'MIC ECHO'],
		[0x02, 'CH1 FADER CURVE'],
		[0x05, 'CH2 FADER CURVE'],
		[0x01, 'CROSSFADER CURVE']
	];
	return [
		...notes.map(([id, label]): ControlHint => ({ source: pioneerNote(ch, id), label })),
		...ccs.map(([id, label]): ControlHint => ({ source: pioneerCc(ch, id), label }))
	];
}

// -------------------------------------------------------------- led rules

/** LED rules exist ONLY for rows whose [PDF] MIDI-OUT column is printed AND
 * whose state the engine exposes as an LedTrigger: START/STOP (D24), AUTO
 * LOOP (D4), SYNC (D10) and the HOT CUE pads. SLIP, CENSOR, KEY LOCK and
 * the rest print MIDI-OUT too but have no engine trigger yet, so no rule. */
function _ledRules(deck: DeckId): LedRule[] {
	const ch = pioneerDeckChannel(deck);
	const padCh = pioneerPadChannel(deck);
	const rules: LedRule[] = [
		// [PDF] D24 START/STOP MIDI-OUT: 9n note 11, OFF=0x00 ON=0x7F.
		{
			trigger: { kind: 'deck_playing', deck },
			out: { ch, note: 0x0b, velocityOn: 0x7f, velocityOff: 0x00 }
		},
		// [PDF] D4 AUTO LOOP MIDI-OUT (LOOP options OFF row): 9n note 20.
		{
			trigger: { kind: 'loop_engaged', deck },
			out: { ch, note: 0x14, velocityOn: 0x7f, velocityOff: 0x00 }
		},
		// [PDF] D10 SYNC MIDI-OUT: 9n note 88.
		{
			trigger: { kind: 'beat_sync_enabled', deck },
			out: { ch, note: 0x58, velocityOn: 0x7f, velocityOff: 0x00 }
		}
	];
	HOT_CUE_SLOTS.forEach((slot, pad) => {
		// [PDF] P5-P12 MIDI-OUT: 9p note = pad note, lit in the colour number sent.
		rules.push({
			trigger: { kind: 'hot_cue_present', deck, slot },
			out: { ch: padCh, note: pad, velocityOn: REV7_PAD_COLOR_ON, velocityOff: REV7_PAD_COLOR_OFF }
		});
	});
	return rules;
}

// ------------------------------------------------------------- device map

export const REV7_MAP: DeviceMap = {
	vendor: 'Pioneer DJ',
	// WebMIDI port names carry the product name, e.g. "DDJ-REV7".
	nameMatch: 'DDJ-REV7',
	bindings: [
		...DECKS.flatMap((deck) => _deckBindings(deck)),
		..._mixerBindings(1),
		..._mixerBindings(2),
		...DECKS.flatMap((deck) => _padBindings(deck)),
		..._globalBindings()
	],
	leds: DECKS.flatMap((deck) => _ledRules(deck)),
	hints: [
		...DECKS.flatMap((deck) => _deckHints(deck)),
		...DECKS.flatMap((deck) => _slotHints(deck)),
		...DECKS.flatMap((deck) => _padHints(deck)),
		..._fxChannelHints(1),
		..._fxChannelHints(2),
		..._globalHints()
	]
};

/** Self-registration: maps/entries.ts discovers this export, so no shared list names this map. */
export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{ key: 'REV7_MAP', map: REV7_MAP, live: true }
];
