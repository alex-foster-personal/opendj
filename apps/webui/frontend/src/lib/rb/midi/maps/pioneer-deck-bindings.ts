import type { ControlHint, LedRule, MidiBinding } from '$lib/rb/midi/midi-types';
import type { DeckId } from '$lib/rb/deck-slots';
import type { HotCueSlot } from '$lib/rb/hot-cue-types';

/** Deck n non-pad controls live on channel n (Pioneer [PDF] channel table). */
export function pioneerDeckChannel(deck: DeckId): number {
	return deck;
}

/** Unshifted performance pads: deck n -> channel 6 + 2*n (Pioneer [PDF] table). */
export function pioneerPadChannel(deck: DeckId): number {
	return 6 + 2 * deck;
}

export function pioneerNote(ch: number, id: number): MidiBinding['source'] {
	return { ch, kind: 'note', id };
}

export function pioneerCc(ch: number, id: number): MidiBinding['source'] {
	return { ch, kind: 'cc', id };
}

/** Shared per-deck transport rows across Pioneer DDJ P0 maps. */
export function pioneerDeckBindings(deck: DeckId, loopExitNote: number): MidiBinding[] {
	const ch = pioneerDeckChannel(deck);
	return [
		{ source: pioneerNote(ch, 0x0b), action: { type: 'deck_play_toggle', deck } },
		{ source: pioneerNote(ch, 0x0c), action: { type: 'deck_cue', deck } },
		{ source: pioneerNote(ch, 0x3f), action: { type: 'shift_modifier' } },
		{ source: pioneerCc(ch, 0x00), action: { type: 'deck_pitch', deck, lsbOffset: 32 } },
		{ source: pioneerNote(ch, 0x14), action: { type: 'deck_beat_loop', deck, beats: 4 } },
		{ source: pioneerNote(ch, loopExitNote), action: { type: 'deck_loop_exit', deck } }
	];
}

/** Hot-cue pad order shared by Pioneer DDJ P0 maps (engine slots A-H). */
const PIONEER_HOT_CUE_SLOTS: readonly HotCueSlot[] = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];

/** Play/cue/loop button LEDs shared by Pioneer DDJ P0 maps. Loop note is per-device. */
export function pioneerTransportLedRules(deck: DeckId, loopLedNote: number): LedRule[] {
	const ch = pioneerDeckChannel(deck);
	return [
		{
			trigger: { kind: 'deck_playing', deck },
			out: { ch, note: 0x0b, velocityOn: 0x7f, velocityOff: 0x00 }
		},
		{
			trigger: { kind: 'deck_loaded', deck },
			out: { ch, note: 0x0c, velocityOn: 0x7f, velocityOff: 0x00 }
		},
		{
			trigger: { kind: 'loop_engaged', deck },
			out: { ch, note: loopLedNote, velocityOn: 0x7f, velocityOff: 0x00 }
		}
	];
}

/** One hot-cue pad LED per slot A-H, on the deck's unshifted pad channel. */
export function pioneerPadLedRules(
	deck: DeckId,
	velocityOn: number,
	velocityOff: number
): LedRule[] {
	const padCh = pioneerPadChannel(deck);
	return PIONEER_HOT_CUE_SLOTS.map((slot, pad): LedRule => ({
		trigger: { kind: 'hot_cue_present', deck, slot },
		out: { ch: padCh, note: pad, velocityOn, velocityOff }
	}));
}

/** Eight hot-cue pads plus eight beat-loop pads on the deck's unshifted pad
 * channel. Pad k sends note k-1 for hot cues and note 0x60+k for beat loops;
 * the loop-length ladder is a software assignment, so the map passes it in. */
export function pioneerPadBindings(deck: DeckId, beatLoopBeats: readonly number[]): MidiBinding[] {
	const ch = pioneerPadChannel(deck);
	const bindings: MidiBinding[] = [];
	for (let pad = 0; pad < 8; pad++) {
		bindings.push({
			source: pioneerNote(ch, pad),
			action: { type: 'deck_hot_cue', deck, slot: PIONEER_HOT_CUE_SLOTS[pad] }
		});
		bindings.push({
			source: pioneerNote(ch, 0x60 + pad),
			action: { type: 'deck_beat_loop', deck, beats: beatLoopBeats[pad] }
		});
	}
	return bindings;
}

/** Two-deck Pioneer browser + global mixer block shared by the DDJ-400 and
 * DDJ-FLX4: channel 7 browse/LOAD, crossfader, master, headphone mix/level and
 * master cue. The FLX10 has four LOAD buttons and its own variant. */
export function pioneerBrowserAndGlobalBindings(): MidiBinding[] {
	const ch = 7;
	return [
		{ source: pioneerCc(ch, 0x40), action: { type: 'browse_encoder' }, relative: true },
		{ source: pioneerCc(ch, 0x64), action: { type: 'browse_encoder' }, relative: true },
		{ source: pioneerNote(ch, 0x46), action: { type: 'browse_load', deck: 1 } },
		{ source: pioneerNote(ch, 0x47), action: { type: 'browse_load', deck: 2 } },
		{ source: pioneerCc(ch, 0x1f), action: { type: 'mixer_global', target: 'crossfader' } },
		{ source: pioneerCc(ch, 0x08), action: { type: 'mixer_global', target: 'master' } },
		{ source: pioneerCc(ch, 0x0c), action: { type: 'headphone_mix' } },
		{ source: pioneerCc(ch, 0x0d), action: { type: 'headphone_level' } },
		{ source: pioneerNote(ch, 0x63), action: { type: 'master_cue', mode: 'latch' } }
	];
}

/** Per-deck hints for the controls both two-deck Pioneer DDJ maps leave
 * unbound. Wire numbers match across the DDJ-400 and DDJ-FLX4; only the
 * printed row names differ, so the caller passes the loop-control labels. */
export function pioneerDeckHints(
	deck: DeckId,
	loopLabels: {
		loopIn: string;
		loopInShift: string;
		loopOut: string;
		loopOutShift: string;
		loopExitShift: string;
	}
): ControlHint[] {
	const ch = pioneerDeckChannel(deck);
	return [
		{ source: pioneerCc(ch, 0x22), label: `JOG platter, vinyl on (deck ${deck})` },
		{ source: pioneerCc(ch, 0x23), label: `JOG platter, vinyl off (deck ${deck})` },
		{ source: pioneerCc(ch, 0x29), label: `JOG platter + SHIFT (deck ${deck})` },
		{ source: pioneerCc(ch, 0x21), label: `JOG wheel side (deck ${deck})` },
		{ source: pioneerNote(ch, 0x36), label: `JOG touch (deck ${deck})` },
		{ source: pioneerNote(ch, 0x67), label: `JOG touch + SHIFT (deck ${deck})` },
		{ source: pioneerNote(ch, 0x58), label: `BEAT SYNC (deck ${deck})` },
		{ source: pioneerNote(ch, 0x5c), label: `BEAT SYNC long press (deck ${deck})` },
		{ source: pioneerNote(ch, 0x60), label: `BEAT SYNC + SHIFT (deck ${deck})` },
		{ source: pioneerNote(ch, 0x10), label: `${loopLabels.loopIn} (deck ${deck})` },
		{ source: pioneerNote(ch, 0x4c), label: `${loopLabels.loopInShift} (deck ${deck})` },
		{ source: pioneerNote(ch, 0x11), label: `${loopLabels.loopOut} (deck ${deck})` },
		{ source: pioneerNote(ch, 0x4e), label: `${loopLabels.loopOutShift} (deck ${deck})` },
		{ source: pioneerNote(ch, 0x50), label: `${loopLabels.loopExitShift} (deck ${deck})` },
		{ source: pioneerNote(ch, 0x51), label: `CUE/LOOP CALL back (deck ${deck})` },
		{ source: pioneerNote(ch, 0x3e), label: `CUE/LOOP CALL back + SHIFT (deck ${deck})` },
		{ source: pioneerNote(ch, 0x53), label: `CUE/LOOP CALL forward (deck ${deck})` },
		{ source: pioneerNote(ch, 0x3d), label: `CUE/LOOP CALL forward + SHIFT (deck ${deck})` },
		{ source: pioneerNote(ch, 0x68), label: `CH CUE + SHIFT (deck ${deck})` }
	];
}
