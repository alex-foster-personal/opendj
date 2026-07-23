/** Cross-panel deck hover focus (deck / mixer / wavestack / library). */
import type { DeckId } from '$lib/rb/types';

export const deckHoverUi = $state({
	deckId: null as DeckId | null
});

export function setHoveredDeck(deckId: DeckId | null): void {
	deckHoverUi.deckId = deckId;
}
