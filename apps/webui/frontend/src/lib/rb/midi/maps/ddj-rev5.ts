/**
 * Pioneer DJ DDJ-REV5 P0 device map (build unit: DDJ-REV5 practice replica, epic #9366).
 *
 * SOURCE OF TRUTH for EVERY Note/CC number in this file: the official
 * AlphaTheta "DDJ-REV5 List of MIDI Messages" PDF (E1, Jul 2023), from
 * https://downloads.support.alphatheta.com/software_info/dj-controllers/DDJ-REV5/DDJ-REV5_MIDI_message_List_E1.pdf
 * (not redistributed here; sha256 recorded in
 * docs/controller/research/DDJ-REV5-MIDI-Message-List.md). Cited below as
 * [PDF] with the sheet's own Fig. ids (D1.., M1.., B1.., E1.., P1..).
 *
 * DO NOT source numbers from tools/deck-diagrams/devices/ddj-rev5/midi.json -
 * that file is `"bootstrap": true` plate-label stubs with invented codes.
 *
 * Channel layout ([PDF] MIDI channel assignment):
 *   deck 1 / deck 2 (non-pad)    -> channels 1 / 2 (decks 3 / 4 -> 3 / 4, see below)
 *   FX (CH1/CH3) / FX (CH2/CH4)  -> channels 5 / 6
 *   BROWSER + global section     -> channel 7 (SHIFT lives here too, [PDF] E12)
 *   performance pads, unshifted  -> channels 8 (deck 1) / 10 (deck 2)
 *   performance pads, shifted    -> channels 9 / 11
 *
 * SHIFT NOTE: the REV5 encodes shift in HARDWARE: a shifted control sends a
 * different data1 and pads move to their own channels. The one SHIFT button
 * sits in the mixer centre on channel 7 note 63 ([PDF] E12) and binds
 * shift_modifier only, so midiState.shiftHeld stays truthful.
 *
 * 14-BIT MIXER CAVEAT (as the DDJ-400 / FLX4 maps): every knob and fader is
 * an MSB/LSB CC pair. Only deck_pitch pairs its LSB; mixer LSB CCs land in
 * the learn log as unmapped. Loud by design.
 *
 * TWO DECKS, NOT FOUR: the REV5 is a 2-channel unit whose DECK 1/3 and 2/4
 * buttons switch each side to a second layer on channels 3 / 4. Nothing here
 * binds decks 3 / 4; the layer switch is a hint.
 *
 * OUT OF CONTRACT (hints only): TEMPO RANGE, KEY LOCK, BPM TRANSITION, SLIP,
 * CENSOR, STEMS buttons, HOT CUE / SCRATCH BANK, DECK 1/3 and 2/4, pad modes
 * other than HOT CUE, PARAMETER arrows, INPUT SELECT, BACK, BOOTH and SAMPLER
 * levels, every Beat FX row, and every shifted twin of a bound row.
 */

import type {
	ControlHint,
	DeviceMap,
	DeviceMapEntry,
	LedRule,
	MidiBinding
} from '$lib/rb/midi/midi-types';
import type { DeckId } from '$lib/rb/deck-slots';
import {
	pioneerCc,
	pioneerDeckChannel,
	pioneerNote,
	pioneerPadChannel,
	pioneerPadLedRules
} from './pioneer-deck-bindings';
import type { HotCueSlot } from '$lib/rb/hot-cue-types';

const DECKS: readonly DeckId[] = [1, 2];

/** [PDF] P5 pad MIDI-OUT: "lit in color specified in color number (1-127)",
 * OFF = 0x00 (dimmer). The colour table is unpublished, so one fixed value. */
export const REV5_PAD_ON = 0x7f;
export const REV5_PAD_OFF = 0x00;

/** [PDF] MIDI channel assignment: BROWSER + global section -> channel 7. */
const GLOBAL_CH = 7;
/** [PDF] M3 FILTER: CH 1 -> CC 23, CH 2 -> CC 24, both on channel 7. */
const FILTER_CC: Record<1 | 2, number> = { 1: 0x17, 2: 0x18 };
const FX_CH: Record<1 | 2, number> = { 1: 5, 2: 6 };

const HOT_CUE_SLOTS: readonly HotCueSlot[] = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];

// ----------------------------------------------------------------- bindings

function _deckBindings(deck: DeckId): MidiBinding[] {
	const ch = pioneerDeckChannel(deck);
	const jog = (cc: number): MidiBinding => ({
		source: pioneerCc(ch, cc),
		action: { type: 'deck_jog', deck },
		relative: true,
		relativeCentered: true
	});
	return [
		// [PDF] D2 PLAY/PAUSE: note 11 (0x0B).
		{ source: pioneerNote(ch, 0x0b), action: { type: 'deck_play_toggle', deck } },
		// [PDF] D1 CUE: note 12 (0x0C).
		{ source: pioneerNote(ch, 0x0c), action: { type: 'deck_cue', deck } },
		// [PDF] D4 TEMPO: CC 0 MSB / CC 32 LSB.
		{ source: pioneerCc(ch, 0x00), action: { type: 'deck_pitch', deck, lsbOffset: 32 } },
		// [PDF] D7 SYNC: note 10 (0x0A).
		{ source: pioneerNote(ch, 0x0a), action: { type: 'deck_sync_toggle', deck } },
		// [PDF] D11 AUTO LOOP: note 4 (0x04), Auto Beat Loop button.
		{ source: pioneerNote(ch, 0x04), action: { type: 'deck_auto_loop_toggle', deck } },
		// [PDF] D12 LOOP 1/2X: note 1 (0x01). [PDF] D13 LOOP 2X: note 3 (0x03).
		{ source: pioneerNote(ch, 0x01), action: { type: 'deck_loop_scale', deck, factor: 0.5 } },
		{ source: pioneerNote(ch, 0x03), action: { type: 'deck_loop_scale', deck, factor: 2 } },
		// [PDF] D16 KEY -: note 42 (0x2A). [PDF] D17 KEY +: note 43 (0x2B).
		{ source: pioneerNote(ch, 0x2a), action: { type: 'deck_key_nudge', deck, semitones: -1 } },
		{ source: pioneerNote(ch, 0x2b), action: { type: 'deck_key_nudge', deck, semitones: 1 } },
		// [PDF] D16 KEY - +SHIFT (printed KEY SYNC): note 58 (0x3A).
		{ source: pioneerNote(ch, 0x3a), action: { type: 'deck_key_sync_toggle', deck } },
		// [PDF] D3 JOG: top CC 34 (vinyl on) / CC 35 (vinyl off), side CC 33.
		// Relative, centred on 64 ("increases from 65, decreases from 63").
		jog(0x22),
		jog(0x23),
		jog(0x21),
		// [PDF] P1 HOT CUE MODE: note 64 (0x40). ROLL / SAVED LOOP / SAMPLER are hints.
		{
			source: pioneerNote(ch, 0x40),
			action: { type: 'controller_pad_mode', deck, mode: 'hot_cue' }
		}
	];
}

function _mixerBindings(deck: 1 | 2): MidiBinding[] {
	const ch = pioneerDeckChannel(deck);
	return [
		// [PDF] M2 TRIM: CC 4 MSB.
		{ source: pioneerCc(ch, 0x04), action: { type: 'mixer_channel', deck, target: 'trim' } },
		// [PDF] M4 EQ HI: CC 7. M5 EQ MID: CC 11. M6 EQ LOW: CC 15.
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
		// [PDF] M13 CH FADER: CC 19 MSB, min at bottom, max at top.
		{ source: pioneerCc(ch, 0x13), action: { type: 'mixer_channel', deck, target: 'fader' } },
		// [PDF] M3 FILTER: channel 7 CC 23 (CH 1) / CC 24 (CH 2).
		{
			source: pioneerCc(GLOBAL_CH, FILTER_CC[deck]),
			action: { type: 'mixer_channel', deck, target: 'filter' }
		},
		// [PDF] M11 / M12 HEADPHONES CUE CH1 / CH2: deck channel note 7 (0x07).
		{ source: pioneerNote(ch, 0x07), action: { type: 'channel_cue', deck } }
	];
}

function _padBindings(deck: DeckId): MidiBinding[] {
	// [PDF] P5 PERFORMANCE PAD 1-8, HOT CUE MODE: pad k sends note k-1 on the pad channel.
	const ch = pioneerPadChannel(deck);
	return HOT_CUE_SLOTS.map((slot, pad): MidiBinding => ({
		source: pioneerNote(ch, pad),
		action: { type: 'deck_hot_cue', deck, slot }
	}));
}

function _globalBindings(): MidiBinding[] {
	const ch = GLOBAL_CH;
	return [
		// [PDF] B1 BROWSE rotate: CC 64, +SHIFT CC 100; relative ticks (CW 1..30, CCW 127..98).
		{ source: pioneerCc(ch, 0x40), action: { type: 'browse_encoder' }, relative: true },
		{ source: pioneerCc(ch, 0x64), action: { type: 'browse_encoder' }, relative: true },
		// [PDF] B3 LOAD: deck 1 note 70 (0x46), deck 2 note 71 (0x47).
		{ source: pioneerNote(ch, 0x46), action: { type: 'browse_load', deck: 1 } },
		{ source: pioneerNote(ch, 0x47), action: { type: 'browse_load', deck: 2 } },
		// [PDF] M14 CROSSFADER: CC 31 MSB, min at left. [PDF] M7 MASTER LEVEL: CC 8 MSB.
		{ source: pioneerCc(ch, 0x1f), action: { type: 'mixer_global', target: 'crossfader' } },
		{ source: pioneerCc(ch, 0x08), action: { type: 'mixer_global', target: 'master' } },
		// [PDF] M23 HEADPHONES MIX: CC 12. [PDF] M22 HEADPHONES LEVEL: CC 13 (front panel).
		{ source: pioneerCc(ch, 0x0c), action: { type: 'headphone_mix' } },
		{ source: pioneerCc(ch, 0x0d), action: { type: 'headphone_level' } },
		// [PDF] E12 SHIFT: channel 7 note 63 (0x3F).
		{ source: pioneerNote(ch, 0x3f), action: { type: 'shift_modifier' } }
	];
}

// -------------------------------------------------------------------- hints

/** Documented-but-unbound deck rows and the shifted twins of bound rows. */
function _deckHints(deck: DeckId): ControlHint[] {
	const ch = pioneerDeckChannel(deck);
	const padCh = pioneerPadChannel(deck);
	const shiftedPadCh = padCh + 1;
	const n = (id: number, label: string): ControlHint => ({
		source: pioneerNote(ch, id),
		label: `${label} (deck ${deck})`
	});
	const c = (id: number, label: string): ControlHint => ({
		source: pioneerCc(ch, id),
		label: `${label} (deck ${deck})`
	});
	return [
		// [PDF] D1 / D2 +SHIFT.
		n(0x1c, 'CUE + SHIFT'),
		n(0x1b, 'PLAY/PAUSE + SHIFT'),
		// [PDF] D3 JOG +SHIFT: CC 31. Touch rows send CC 13 / 28 with 9n status; not modelled.
		c(0x1f, 'JOG + SHIFT'),
		// [PDF] D4 TEMPO +SHIFT: CC 5 MSB.
		c(0x05, 'TEMPO + SHIFT'),
		// [PDF] D5 TEMPO RANGE: note 8, +SHIFT note 24.
		n(0x08, 'TEMPO RANGE'),
		n(0x18, 'TEMPO RANGE + SHIFT'),
		// [PDF] D6 KEY LOCK: note 9, +SHIFT note 25.
		n(0x09, 'KEY LOCK'),
		n(0x19, 'KEY LOCK + SHIFT'),
		// [PDF] D7 SYNC +SHIFT: note 26.
		n(0x1a, 'SYNC + SHIFT'),
		// [PDF] D10 BPM TRANSITION START: note 49, +SHIFT note 51.
		n(0x31, 'BPM TRANSITION START'),
		n(0x33, 'BPM TRANSITION START + SHIFT'),
		// [PDF] D11 AUTO LOOP +SHIFT: note 20. D12 / D13 +SHIFT: notes 16 / 18.
		n(0x14, 'AUTO LOOP + SHIFT'),
		n(0x10, 'LOOP 1/2X + SHIFT'),
		n(0x12, 'LOOP 2X + SHIFT'),
		// [PDF] D14 SLIP: note 40, +SHIFT 56. D15 CENSOR: note 41, +SHIFT 57.
		n(0x28, 'SLIP'),
		n(0x38, 'SLIP + SHIFT'),
		n(0x29, 'CENSOR'),
		n(0x39, 'CENSOR + SHIFT'),
		// [PDF] D17 KEY + +SHIFT (printed KEY RESET): note 59.
		n(0x3b, 'KEY RESET'),
		// [PDF] D18 STEMS: note 37, +SHIFT 53.
		n(0x25, 'STEMS'),
		n(0x35, 'STEMS + SHIFT'),
		// [PDF] D20-D23 VOCAL / MELODY / BASS / DRUMS in STEMS MODE: notes 44-47.
		n(0x2c, 'VOCAL stem'),
		n(0x2d, 'MELODY stem'),
		n(0x2e, 'BASS stem'),
		n(0x2f, 'DRUMS stem'),
		// [PDF] D24 HOT CUE: note 38, +SHIFT 54. D25 SCRATCH BANK: note 39, +SHIFT 55.
		n(0x26, 'HOT CUE'),
		n(0x36, 'HOT CUE + SHIFT'),
		n(0x27, 'SCRATCH BANK'),
		n(0x37, 'SCRATCH BANK + SHIFT'),
		// [PDF] P1 HOT CUE MODE +SHIFT (PITCH PLAY): note 80. P2-P4 pad modes and shifted twins.
		n(0x50, 'PITCH PLAY MODE'),
		n(0x41, 'ROLL MODE'),
		n(0x51, 'PIANO PLAY MODE'),
		n(0x42, 'SAVED LOOP MODE'),
		n(0x52, 'STEMS MODE'),
		n(0x43, 'SAMPLER MODE'),
		n(0x53, 'SCRATCH BANK MODE'),
		// [PDF] P6 / P7 PARAMETER 1 arrows in HOT CUE MODE: notes 96 / 112, +SHIFT 72 / 90.
		n(0x60, 'PARAMETER 1 back'),
		n(0x48, 'PARAMETER 1 back + SHIFT'),
		n(0x70, 'PARAMETER 1 forward'),
		n(0x5a, 'PARAMETER 1 forward + SHIFT'),
		// [PDF] M1 INPUT SELECT: note 14 (USB A position).
		n(0x0e, 'INPUT SELECT'),
		// [PDF] M11 / M12 HEADPHONES CUE +SHIFT: note 31.
		n(0x1f, 'HEADPHONES CUE + SHIFT'),
		// [PDF] D26 DECK 1/3 (deck 1) and DECK 2/4 (deck 2): channel 3 / 4 note 6.
		{ source: pioneerNote(deck + 2, 0x06), label: `DECK ${deck}/${deck + 2} layer switch` },
		// [PDF] P5 pads +SHIFT: own channel, same notes.
		...HOT_CUE_SLOTS.map((_slot, pad): ControlHint => ({
			source: pioneerNote(shiftedPadCh, pad),
			label: `PAD ${pad + 1} + SHIFT (deck ${deck})`
		}))
	];
}

function _globalHints(): ControlHint[] {
	const ch = GLOBAL_CH;
	return [
		// [PDF] B1 BROWSE press: note 65, +SHIFT 66. B2 BACK: note 101, +SHIFT 102.
		{ source: pioneerNote(ch, 0x41), label: 'BROWSE press' },
		{ source: pioneerNote(ch, 0x42), label: 'BROWSE press + SHIFT' },
		{ source: pioneerNote(ch, 0x65), label: 'BACK' },
		{ source: pioneerNote(ch, 0x66), label: 'BACK + SHIFT' },
		// [PDF] B3 LOAD +SHIFT: deck 1 note 93, deck 2 note 109.
		{ source: pioneerNote(ch, 0x5d), label: 'LOAD deck 1 + SHIFT' },
		{ source: pioneerNote(ch, 0x6d), label: 'LOAD deck 2 + SHIFT' },
		// [PDF] D9 BPM TRANSITION SELECT: note 62; +SHIFT note 48 shares the FX1-1 + SHIFT row.
		{ source: pioneerNote(ch, 0x3e), label: 'BPM TRANSITION SELECT' },
		{ source: pioneerNote(ch, 0x30), label: 'FX1-1 + SHIFT / BPM TRANSITION SELECT + SHIFT' },
		// [PDF] E1-E6 +SHIFT: channel 7 notes 49-53 (48 above).
		{ source: pioneerNote(ch, 0x31), label: 'FX1-2 + SHIFT' },
		{ source: pioneerNote(ch, 0x32), label: 'FX1-3 + SHIFT' },
		{ source: pioneerNote(ch, 0x33), label: 'FX2-1 + SHIFT' },
		{ source: pioneerNote(ch, 0x34), label: 'FX2-2 + SHIFT' },
		{ source: pioneerNote(ch, 0x35), label: 'FX2-3 + SHIFT' },
		// [PDF] E7 / E8 +SHIFT (AUTO BPM / TAP): notes 54 / 55.
		{ source: pioneerNote(ch, 0x36), label: 'BEAT back + SHIFT (AUTO BPM)' },
		{ source: pioneerNote(ch, 0x37), label: 'BEAT forward + SHIFT (TAP)' },
		// [PDF] D26 +SHIFT (Dual Deck): notes 124 / 125.
		{ source: pioneerNote(ch, 0x7c), label: 'DECK 1/3 + SHIFT (dual deck)' },
		{ source: pioneerNote(ch, 0x7d), label: 'DECK 2/4 + SHIFT (dual deck)' },
		// [PDF] M8 BOOTH LEVEL: CC 9. M9 SAMPLER LEVEL: CC 3.
		{ source: pioneerCc(ch, 0x09), label: 'BOOTH LEVEL' },
		{ source: pioneerCc(ch, 0x03), label: 'SAMPLER LEVEL' },
		// [PDF] E1-E3 FX1-1/2/3: channel 5 notes 112-114. E4-E6 FX2-1/2/3: channel 6.
		...([1, 2] as const).flatMap((unit) =>
			[0x70, 0x71, 0x72].map((note, i): ControlHint => ({
				source: pioneerNote(FX_CH[unit], note),
				label: `FX${unit}-${i + 1} select`
			}))
		),
		// [PDF] E7 BEAT back / E8 BEAT forward: notes 6 / 7 on the selected FX unit's channel.
		{ source: pioneerNote(5, 0x06), label: 'BEAT back (FX1)' },
		{ source: pioneerNote(5, 0x07), label: 'BEAT forward (FX1)' },
		{ source: pioneerNote(6, 0x06), label: 'BEAT back (FX2)' },
		{ source: pioneerNote(6, 0x07), label: 'BEAT forward (FX2)' },
		// [PDF] E9 / E10 CH1 / CH2 EFFECT LEVER: channel 5 / 6 note 80.
		{ source: pioneerNote(5, 0x50), label: 'CH1 EFFECT LEVER' },
		{ source: pioneerNote(6, 0x50), label: 'CH2 EFFECT LEVER' },
		// [PDF] E11 LEVEL/DEPTH: CC 2 MSB on the selected FX unit's channel.
		{ source: pioneerCc(5, 0x02), label: 'FX LEVEL/DEPTH (FX1)' },
		{ source: pioneerCc(6, 0x02), label: 'FX LEVEL/DEPTH (FX2)' }
	];
}

// ---------------------------------------------------------------- led rules

/** LED rules exist ONLY for rows whose [PDF] MIDI-OUT column prints a status. */
function _ledRules(deck: DeckId): LedRule[] {
	const ch = pioneerDeckChannel(deck);
	const stems: ReadonlyArray<[number, 'vocal' | 'other' | 'bass' | 'drums']> = [
		[0x2c, 'vocal'],
		[0x2d, 'other'],
		[0x2e, 'bass'],
		[0x2f, 'drums']
	];
	return [
		// [PDF] D2 PLAY/PAUSE MIDI-OUT 9n 0B. [PDF] D1 CUE MIDI-OUT 9n 0C (lit when loaded).
		{
			trigger: { kind: 'deck_playing', deck },
			out: { ch, note: 0x0b, velocityOn: 0x7f, velocityOff: 0x00 }
		},
		{
			trigger: { kind: 'deck_loaded', deck },
			out: { ch, note: 0x0c, velocityOn: 0x7f, velocityOff: 0x00 }
		},
		// [PDF] D11 AUTO LOOP MIDI-OUT 9n 04. [PDF] D7 SYNC MIDI-OUT 9n 0A.
		{
			trigger: { kind: 'loop_engaged', deck },
			out: { ch, note: 0x04, velocityOn: 0x7f, velocityOff: 0x00 }
		},
		{
			trigger: { kind: 'beat_sync_enabled', deck },
			out: { ch, note: 0x0a, velocityOn: 0x7f, velocityOff: 0x00 }
		},
		// [PDF] P1 HOT CUE MODE MIDI-OUT 9n 40.
		{
			trigger: { kind: 'pad_mode_selected', deck, mode: 'hot_cue' },
			out: { ch, note: 0x40, velocityOn: 0x7f, velocityOff: 0x00 }
		},
		// [PDF] M11 / M12 HEADPHONES CUE MIDI-OUT 9n 07.
		{
			trigger: { kind: 'channel_cue_enabled', deck },
			out: { ch, note: 0x07, velocityOn: 0x7f, velocityOff: 0x00 }
		},
		// [PDF] D20-D23 STEMS MODE MIDI-OUT 9n 2C-2F.
		...stems.map(([note, stem]): LedRule => ({
			trigger: { kind: 'stem_active', deck, stem },
			out: { ch, note, velocityOn: 0x7f, velocityOff: 0x00 }
		})),
		// [PDF] P5 pad MIDI-OUT 9p note = pad note, colour number 1-127.
		...pioneerPadLedRules(deck, REV5_PAD_ON, REV5_PAD_OFF)
	];
}

// --------------------------------------------------------------- device map

export const REV5_MAP: DeviceMap = {
	vendor: 'Pioneer DJ',
	// WebMIDI port names carry the product name, e.g. "DDJ-REV5".
	nameMatch: 'DDJ-REV5',
	bindings: [
		...DECKS.flatMap((deck) => _deckBindings(deck)),
		...([1, 2] as const).flatMap((deck) => _mixerBindings(deck)),
		...DECKS.flatMap((deck) => _padBindings(deck)),
		..._globalBindings()
	],
	leds: DECKS.flatMap((deck) => _ledRules(deck)),
	hints: [...DECKS.flatMap((deck) => _deckHints(deck)), ..._globalHints()]
};

/** Self-registration: maps/entries.ts discovers this export, so no shared list names this map. */
export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{ key: 'REV5_MAP', map: REV5_MAP, live: true }
];
