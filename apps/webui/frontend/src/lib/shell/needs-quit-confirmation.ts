import type { DeckId } from '$lib/rb/deck-id';
import type { PerformanceState } from '$lib/rb/performance-ipc.svelte';

const DECK_IDS: DeckId[] = [1, 2, 3, 4];

function anyDeckPlaying(state: PerformanceState): boolean {
	for (const deckId of DECK_IDS) {
		const deck = state.decks[deckId];
		if (deck.playing || deck.audible) return true;
	}
	return false;
}

function anyDeckLoaded(state: PerformanceState): boolean {
	for (const deckId of DECK_IDS) {
		if (state.decks[deckId].stable_id !== null) return true;
	}
	return false;
}

/** INSTALL-21: skip the quit dialog only when nothing is loaded and nothing plays. */
export function needsQuitConfirmation(state: PerformanceState): boolean {
	if (anyDeckPlaying(state)) return true;
	if (anyDeckLoaded(state)) return true;
	return false;
}
