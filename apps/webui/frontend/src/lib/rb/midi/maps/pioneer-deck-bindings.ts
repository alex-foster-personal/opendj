import type { MidiBinding } from '$lib/rb/midi/midi-types';
import type { DeckId } from '$lib/rb/deck-slots';

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
