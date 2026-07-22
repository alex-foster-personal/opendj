/**
 * Pioneer DJ DDJ-FLX10 P0 device map (build unit: FLX10 map).
 *
 * SOURCE OF TRUTH for EVERY Note/CC number in this file: the official
 * Pioneer DJ / AlphaTheta "DDJ-FLX10 List of MIDI message" PDF (E1, 2023),
 * https://downloads.support.alphatheta.com/software_info/dj-controllers/DDJ-FLX10/DDJ-FLX10_MIDI_Message_List_E1.pdf
 * - cited throughout as [PDF]. This is source [1] of SPIKE-CONTROLLERS.md
 * (rekordbox-parity spikes); protocol-shape findings (no SysEx, channel
 * partitioning, relative encoder ticks, 14-bit tempo pairs, RGB pad LEDs
 * as Note-On velocity) are spike section 2a. Row labels (D1, M3, B1, P1..)
 * are the PDF's own Fig. identifiers; page numbers are the PDF's.
 *
 * Channel layout ([PDF] p.1 "MIDI channel assignment" table):
 *   decks 1-4 (non-pad)          -> channels 1-4
 *   browser + global mixer       -> channel 7
 *   performance pads, unshifted  -> channels 8/10/12/14 (deck 1/2/3/4)
 *   performance pads, shifted    -> channels 9/11/13/15 (out of P0 scope)
 *
 * SHIFT NOTE: the FLX10 encodes shift in HARDWARE - while SHIFT is held
 * the same physical control sends a DIFFERENT data1 (e.g. PLAY sends note
 * 71 instead of 11, [PDF] p.2 D1), and pads move to their own channels.
 * So this map needs NO `shift: true` bindings; shifted-message ids are
 * bound (or intentionally left unmapped -> learn log) as their own
 * sources. The SHIFT button itself is still bound to shift_modifier so
 * midiState.shiftHeld stays truthful for UI display.
 *
 * 14-BIT MIXER CAVEAT: every FLX10 mixer control (trim/EQ/fader/
 * crossfader/master) is an MSB/LSB CC pair ([PDF] p.2 M1-M8). The P0
 * contract pairs 14-bit CCs for deck_pitch ONLY, so mixer controls bind
 * their MSB CC (full 7-bit resolution, plenty for P0) and the LSB CCs
 * (36/39/43/47/51 on ch 1-4, 63/40 on ch 7) intentionally land in the
 * learn log as unmapped. Loud by design; widen the contract if 14-bit
 * mixer resolution is ever wanted.
 *
 * OUT OF CONTRACT (cited numbers preserved in comments for the future
 * contract widening, NOT bound): LOOP IN.1/2X note 16 / shift 76 ([PDF]
 * p.2 D14), LOOP OUT.2X note 17 / shift 77 (D15) - the P0 MidiAction
 * union has no loop_in/loop_out/halve/double cases. HOT CUE PAGE2 pads
 * (notes 8-15, [PDF] p.4-7) - HotCueSlot models 8 slots (A-H, PAGE1).
 *
 * Requirements (mini-PRD):
 *   ✔︎ Transport play/cue per deck ([PDF] D1/D2).
 *     [if] note 0x0B ch 1 arrives [then] deck_play_toggle deck 1
 *   ✔︎ 32 hot-cue pads: PAGE1 notes 0-7 x pad channels 8/10/12/14 -> slots
 *     A-H ([PDF] p.4-7 P1-P8 HOT CUE mode PAGE1).
 *     [if] note 3 ch 12 arrives [then] deck_hot_cue deck 3 slot D
 *   ✔︎ 32 beat-loop pads: PAGE1 notes 96-103 -> beats table ([PDF] P1-P8
 *     BEAT LOOP mode PAGE1); 4 BEAT/EXIT + shifted twin ([PDF] D16).
 *   ✔︎ Mixer per channel (trim/EQ/fader) + crossfader + master on MSB CCs
 *     ([PDF] M1-M8); browse encoder relative + LOAD 1-4 ([PDF] B1).
 *   ✔︎ Tempo fader as true 14-bit MSB/LSB pair via deck_pitch lsbOffset 32
 *     ([PDF] D4: CC 0 MSB / CC 32 LSB).
 *     [if] MSB then LSB arrive on ch 2 CC 0/32 [then ⛔️] anything but ONE
 *       continuous14 deck_pitch emit for deck 2 is broken
 *   ✔︎ LED rules: play/cue/loop button LEDs + hot-cue pad RGB velocity
 *     ([PDF] MIDI-OUT columns; pad colour = velocity 1-127).
 *   ✔︎ Zero fabricated numbers: each block comments its [PDF] Fig. row.
 */

import type { ControlHint, DeviceMap, LedRule, MidiBinding } from '$lib/rb/midi/midi-types';
import type { DeckId, HotCueSlot } from '$lib/rb/types';

// -------------------------------------------------------------- constants

const DECKS: readonly DeckId[] = [1, 2, 3, 4];

/** Hot-cue pad order: pad k triggers slot HOT_CUE_SLOTS[k-1]. Wire fact:
 * PAGE1 pad k sends note k-1 ([PDF] p.4-7, P1..P8 HOT CUE mode PAGE1 rows:
 * notes 0,1,2,3,4,5,6,7). Slot lettering is our engine's A-H convention. */
const HOT_CUE_SLOTS: readonly HotCueSlot[] = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];

/** Beats per BEAT LOOP pad 1..8. The [PDF] documents only the wire notes
 * (96-103); loop LENGTH per pad is software-side (rekordbox assigns its
 * own), so this table is OUR software assignment - rekordbox-style doubling
 * ladder centred on 4 beats. */
export const FLX10_BEAT_LOOP_PAD_BEATS: readonly number[] = [0.25, 0.5, 1, 2, 4, 8, 16, 32];

/** Pad LED "on" colour number. [PDF] pad rows (p.4-7 MIDI-OUT): "lit in
 * color specified in color number(1-127). OFF=0x00(dimmer)". 0x7F is a
 * valid colour number; WHICH colour each 1-127 value shows is not
 * published anywhere in the [PDF], so P0 uses one fixed value. */
export const FLX10_PAD_COLOR_ON = 0x7f;
export const FLX10_PAD_COLOR_OFF = 0x00;

/** Our cue-colour -> FLX10 pad colour number (Note-On velocity) resolver,
 * for the future per-cue LED palette pass (LedRule is static per rule in
 * P0, so this is exported scaffolding, not yet wired). The [PDF] documents
 * ONLY that velocity 1-127 selects a palette entry - the index-to-colour
 * table itself is unpublished, so mapping rekordbox color_table_index
 * values to specific velocities would be fabrication until verified on
 * the real unit. Until then every index resolves to FLX10_PAD_COLOR_ON. */
export function flx10PadColorVelocity(colorTableIndex: number | null): number {
	void colorTableIndex; // unpublished palette - see docstring
	return FLX10_PAD_COLOR_ON;
}

// ---------------------------------------------------------------- _helpers

/** Deck n non-pad controls live on channel n ([PDF] p.1 channel table). */
function _deckCh(deck: DeckId): number {
	return deck;
}

/** Unshifted performance pads: deck 1/2/3/4 -> channel 8/10/12/14
 * ([PDF] p.1 channel table). */
function _padCh(deck: DeckId): number {
	return 6 + 2 * deck;
}

function _note(ch: number, id: number): MidiBinding['source'] {
	return { ch, kind: 'note', id };
}

function _cc(ch: number, id: number): MidiBinding['source'] {
	return { ch, kind: 'cc', id };
}

// ---------------------------------------------------- per-deck deck section

function _deckBindings(deck: DeckId): MidiBinding[] {
	const ch = _deckCh(deck);
	return [
		// [PDF] p.2 D1 PLAY/PAUSE: 9n note 11 (0x0B).
		{ source: _note(ch, 0x0b), action: { type: 'deck_play_toggle', deck } },
		// [PDF] p.2 D2 CUE: 9n note 12 (0x0C).
		{ source: _note(ch, 0x0c), action: { type: 'deck_cue', deck } },
		// [PDF] p.2 D25 SHIFT: 9n note 63 (0x3F).
		{ source: _note(ch, 0x3f), action: { type: 'shift_modifier' } },
		// [PDF] p.2 D4 TEMPO: CC 0 MSB / CC 32 (0x20) LSB, "-" side = Min,
		// "+" side = Max. Glue convention is 0 -> -range, 1 -> +range, so
		// the wire orientation matches directly (no invert).
		{ source: _cc(ch, 0x00), action: { type: 'deck_pitch', deck, lsbOffset: 32 } },
		// [PDF] p.2 D16 "4 BEAT / EXIT": 9n note 20 (0x14). Engages a
		// 4-beat auto loop (software semantics: ours; wire number: [PDF]).
		{ source: _note(ch, 0x14), action: { type: 'deck_beat_loop', deck, beats: 4 } },
		// [PDF] p.2 D16 +SHIFT: 9n note 80 (0x50) - hardware-encoded
		// shift twin of 4 BEAT/EXIT. Software semantics (ours): loop exit.
		{ source: _note(ch, 0x50), action: { type: 'deck_loop_exit', deck } }
		// NOT BOUND (P0 contract has no loop_in/out/halve/double actions):
		//   [PDF] p.2 D14 LOOP IN.1/2X: note 16 (0x10), +SHIFT note 76 (0x4C)
		//   [PDF] p.2 D15 LOOP OUT.2X: note 17 (0x11), +SHIFT note 77 (0x4D)
	];
}

// -------------------------------------------------- per-deck mixer section

function _mixerBindings(deck: DeckId): MidiBinding[] {
	const ch = _deckCh(deck);
	return [
		// [PDF] p.2 M3 TRIM: CC 4 (0x04) MSB (LSB 36 unbound - header note).
		{ source: _cc(ch, 0x04), action: { type: 'mixer_channel', deck, target: 'trim' } },
		// [PDF] p.2 M4 EQ HI: CC 7 (0x07) MSB (LSB 39 unbound).
		{ source: _cc(ch, 0x07), action: { type: 'mixer_channel', deck, target: 'eq', band: 'high' } },
		// [PDF] p.2 M5 EQ MID: CC 11 (0x0B) MSB (LSB 43 unbound).
		{ source: _cc(ch, 0x0b), action: { type: 'mixer_channel', deck, target: 'eq', band: 'mid' } },
		// [PDF] p.2 M6 EQ LOW: CC 15 (0x0F) MSB (LSB 47 unbound).
		{ source: _cc(ch, 0x0f), action: { type: 'mixer_channel', deck, target: 'eq', band: 'low' } },
		// [PDF] p.2 M2 CH FADER: CC 19 (0x13) MSB (LSB 51 unbound), Min at
		// bottom / Max at top - matches setFader 0..1, no invert.
		{ source: _cc(ch, 0x13), action: { type: 'mixer_channel', deck, target: 'fader' } }
	];
}

// -------------------------------------------------------- per-deck pad bank

function _padBindings(deck: DeckId): MidiBinding[] {
	const ch = _padCh(deck);
	const bindings: MidiBinding[] = [];
	for (let pad = 0; pad < 8; pad++) {
		// [PDF] p.4-7 P1..P8, HOT CUE mode PAGE1: pad k -> note k-1 (0..7).
		bindings.push({
			source: _note(ch, pad),
			action: { type: 'deck_hot_cue', deck, slot: HOT_CUE_SLOTS[pad] }
		});
		// [PDF] p.4-7 P1..P8, BEAT LOOP mode PAGE1: pad k -> note 95+k
		// (96..103 / 0x60..0x67). Beats per pad: our table (see const).
		bindings.push({
			source: _note(ch, 0x60 + pad),
			action: { type: 'deck_beat_loop', deck, beats: FLX10_BEAT_LOOP_PAD_BEATS[pad] }
		});
	}
	return bindings;
}

// -------------------------------------------------- browser + global mixer

function _browserAndGlobalBindings(): MidiBinding[] {
	// Browser + global mixer share channel 7 ([PDF] p.1 channel table).
	const ch = 7;
	return [
		// [PDF] p.1 B1 BROWSE rotate: CC 64 (0x40), relative ticks
		// (CW 0x01..0x1E, CCW 0x7F..0x62 - spike 2a).
		{ source: _cc(ch, 0x40), action: { type: 'browse_encoder' }, relative: true },
		// [PDF] p.1 B1 BROWSE rotate +SHIFT: CC 100 (0x64), same tick
		// encoding. Software choice (ours): same selection movement.
		{ source: _cc(ch, 0x64), action: { type: 'browse_encoder' }, relative: true },
		// [PDF] p.1 B1 BROWSE press = LOAD, per selected deck:
		// deck1 note 70 (0x46), deck2 71 (0x47), deck3 72 (0x48),
		// deck4 73 (0x49).
		{ source: _note(ch, 0x46), action: { type: 'browse_load', deck: 1 } },
		{ source: _note(ch, 0x47), action: { type: 'browse_load', deck: 2 } },
		{ source: _note(ch, 0x48), action: { type: 'browse_load', deck: 3 } },
		{ source: _note(ch, 0x49), action: { type: 'browse_load', deck: 4 } },
		// [PDF] p.2 M1 CROSSFADER: CC 31 (0x1F) MSB (LSB 63 unbound),
		// Min at left / Max at right - engine x=0 is full A (left), no invert.
		{ source: _cc(ch, 0x1f), action: { type: 'mixer_global', target: 'crossfader' } },
		// [PDF] p.2 M8 MASTER LEVEL: CC 8 (0x08) MSB (LSB 40 unbound).
		{ source: _cc(ch, 0x08), action: { type: 'mixer_global', target: 'master' } }
	];
}

// -------------------------------------------------------------- led rules

/** Documented-but-unbound loop buttons ([PDF] p.2 D14/D15). These are real
 * physical buttons with NO P0 action (the union has no loop_in/out cases),
 * so a user pressing them sees only learn-log traffic - the hint names what
 * they pressed. Wire numbers are the SAME ones cited in _deckBindings' NOT
 * BOUND comment; the shift twins are hardware-encoded (own note, same ch). */
function _deckHints(deck: DeckId): ControlHint[] {
	const ch = _deckCh(deck);
	return [
		// [PDF] p.2 D14 LOOP IN.1/2X: note 16 (0x10), +SHIFT note 76 (0x4C).
		{ source: _note(ch, 0x10), label: `LOOP IN (deck ${deck})` },
		{ source: _note(ch, 0x4c), label: `LOOP IN + SHIFT (deck ${deck})` },
		// [PDF] p.2 D15 LOOP OUT.2X: note 17 (0x11), +SHIFT note 77 (0x4D).
		{ source: _note(ch, 0x11), label: `LOOP OUT (deck ${deck})` },
		{ source: _note(ch, 0x4d), label: `LOOP OUT + SHIFT (deck ${deck})` }
	];
}

function _ledRules(deck: DeckId): LedRule[] {
	const ch = _deckCh(deck);
	const padCh = _padCh(deck);
	const rules: LedRule[] = [
		// [PDF] p.2 D1 PLAY/PAUSE MIDI-OUT: 9n note 11 (0x0B), OFF=0x00
		// ON=0x7F. Lit while the deck plays (spike: play-button feedback).
		{
			trigger: { kind: 'deck_playing', deck },
			out: { ch, note: 0x0b, velocityOn: 0x7f, velocityOff: 0x00 }
		},
		// [PDF] p.2 D2 CUE MIDI-OUT: 9n note 12 (0x0C). Software choice
		// (ours): lit when a track is loaded (cue point exists to return to).
		{
			trigger: { kind: 'deck_loaded', deck },
			out: { ch, note: 0x0c, velocityOn: 0x7f, velocityOff: 0x00 }
		},
		// [PDF] p.2 D16 4 BEAT/EXIT MIDI-OUT: 9n note 20 (0x14). Lit while
		// a loop is engaged.
		{
			trigger: { kind: 'loop_engaged', deck },
			out: { ch, note: 0x14, velocityOn: 0x7f, velocityOff: 0x00 }
		}
	];
	for (let pad = 0; pad < 8; pad++) {
		// [PDF] p.4-7 pad MIDI-OUT: 9p note = pad note, velocity = colour
		// number 1-127 (OFF=0x00 dimmer). Lit when the hot-cue slot is
		// populated; per-cue colour resolution is P1 (flx10PadColorVelocity).
		rules.push({
			trigger: { kind: 'hot_cue_present', deck, slot: HOT_CUE_SLOTS[pad] },
			out: { ch: padCh, note: pad, velocityOn: FLX10_PAD_COLOR_ON, velocityOff: FLX10_PAD_COLOR_OFF }
		});
	}
	return rules;
}

// ------------------------------------------------------------- device map

export const FLX10_MAP: DeviceMap = {
	vendor: 'Pioneer DJ',
	// WebMIDI port names carry the product name, e.g. "DDJ-FLX10".
	nameMatch: 'DDJ-FLX10',
	bindings: [
		...DECKS.flatMap((deck) => _deckBindings(deck)),
		...DECKS.flatMap((deck) => _mixerBindings(deck)),
		...DECKS.flatMap((deck) => _padBindings(deck)),
		..._browserAndGlobalBindings()
	],
	leds: DECKS.flatMap((deck) => _ledRules(deck)),
	hints: DECKS.flatMap((deck) => _deckHints(deck))
};
