import type { DeckId } from '$lib/rb/deck-slots';

/** Most recently loaded / played / loop-targeted deck for Space play/pause. */
let lastRecentDeck: DeckId | null = null;

export function noteRecentDeck(deck: DeckId): void {
	lastRecentDeck = deck;
}

export function getRecentDeck(): DeckId | null {
	return lastRecentDeck;
}
